from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple, Type, Union
from uuid import UUID

from sqlalchemy import (
    Column, Integer, LargeBinary, MetaData, Table, Unicode, and_, bindparam, or_, select)
from sqlalchemy.engine import URL
from sqlalchemy.exc import CompileError, IntegrityError
from sqlalchemy.future import Engine, create_engine
from sqlalchemy.sql.ddl import DropTable
from sqlalchemy.sql.elements import BindParameter

from ...abc import DataStore, Job, Schedule, Serializer
from ...events import (
    Event, EventHub, JobAdded, JobDeserializationFailed, ScheduleAdded,
    ScheduleDeserializationFailed, ScheduleRemoved, ScheduleUpdated, SubscriptionToken)
from ...exceptions import ConflictingIdError, SerializationError
from ...policies import ConflictPolicy
from ...serializers.pickle import PickleSerializer
from ...structures import JobResult
from ...util import reentrant

logger = logging.getLogger(__name__)


@reentrant
class SQLAlchemyDataStore(DataStore):
    def __init__(self, engine: Engine, *, schema: Optional[str] = None,
                 serializer: Optional[Serializer] = None,
                 lock_expiration_delay: float = 30, max_poll_time: Optional[float] = 1,
                 max_idle_time: float = 60, start_from_scratch: bool = False):
        self.engine = engine
        self.schema = schema
        self.serializer = serializer or PickleSerializer()
        self.lock_expiration_delay = lock_expiration_delay
        self.max_poll_time = max_poll_time
        self.max_idle_time = max_idle_time
        self.start_from_scratch = start_from_scratch
        self._logger = logging.getLogger(__name__)
        self._events = EventHub()

        # Generate the table definitions
        self._metadata = self.get_table_definitions()
        self.t_metadata = self._metadata.tables['metadata']
        self.t_schedules = self._metadata.tables['schedules']
        self.t_jobs = self._metadata.tables['jobs']
        self.t_job_results = self._metadata.tables['job_results']

        # Find out if the dialect supports RETURNING
        update = self.t_jobs.update().returning(self.t_schedules.c.id)
        try:
            update.compile(bind=self.engine)
        except CompileError:
            self._supports_update_returning = False
        else:
            self._supports_update_returning = True

    @classmethod
    def from_url(cls, url: Union[str, URL], **options) -> 'SQLAlchemyDataStore':
        engine = create_engine(url)
        return cls(engine, **options)

    def __enter__(self):
        with self.engine.begin() as conn:
            if self.start_from_scratch:
                for table in self._metadata.sorted_tables:
                    conn.execute(DropTable(table, if_exists=True))

            self._metadata.create_all(conn)
            query = select(self.t_metadata.c.schema_version)
            result = conn.execute(query)
            version = result.scalar()
            if version is None:
                conn.execute(self.t_metadata.insert(values={'schema_version': 1}))
            elif version > 1:
                raise RuntimeError(f'Unexpected schema version ({version}); '
                                   f'only version 1 is supported by this version of APScheduler')

        self._events.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._events.__exit__(exc_type, exc_val, exc_tb)

    def get_table_definitions(self) -> MetaData:
        if self.engine.dialect.name in ('mysql', 'mariadb'):
            from sqlalchemy.dialects.mysql import TIMESTAMP
            timestamp_type = TIMESTAMP(fsp=6)
        else:
            from sqlalchemy.types import TIMESTAMP
            timestamp_type = TIMESTAMP(timezone=True)

        metadata = MetaData()
        Table(
            'metadata',
            metadata,
            Column('schema_version', Integer, nullable=False)
        )
        Table(
            'tasks',
            metadata,
            Column('id', Unicode(500), primary_key=True),
            Column('serialized_data', LargeBinary, nullable=False)
        )
        Table(
            'schedules',
            metadata,
            Column('id', Unicode(500), primary_key=True),
            Column('task_id', Unicode(500), nullable=False, index=True),
            Column('serialized_data', LargeBinary, nullable=False),
            Column('next_fire_time', timestamp_type, index=True),
            Column('acquired_by', Unicode(500)),
            Column('acquired_until', timestamp_type)
        )
        Table(
            'jobs',
            metadata,
            Column('id', Unicode(32), primary_key=True),
            Column('task_id', Unicode(500), nullable=False, index=True),
            Column('serialized_data', LargeBinary, nullable=False),
            Column('created_at', timestamp_type, nullable=False),
            Column('acquired_by', Unicode(500)),
            Column('acquired_until', timestamp_type)
        )
        Table(
            'job_results',
            metadata,
            Column('job_id', Unicode(32), primary_key=True),
            Column('finished_at', timestamp_type, index=True),
            Column('serialized_data', LargeBinary, nullable=False)
        )
        return metadata

    def _deserialize_jobs(self, serialized_jobs: Iterable[Tuple[UUID, bytes]]) -> List[Job]:
        jobs: List[Job] = []
        for job_id, serialized_data in serialized_jobs:
            try:
                jobs.append(self.serializer.deserialize(serialized_data))
            except SerializationError as exc:
                self._events.publish(JobDeserializationFailed(job_id=job_id, exception=exc))

        return jobs

    def _deserialize_schedules(
            self, serialized_schedules: Iterable[Tuple[str, bytes]]) -> List[Schedule]:
        jobs: List[Schedule] = []
        for schedule_id, serialized_data in serialized_schedules:
            try:
                jobs.append(self.serializer.deserialize(serialized_data))
            except SerializationError as exc:
                self._events.publish(
                    ScheduleDeserializationFailed(schedule_id=schedule_id, exception=exc))

        return jobs

    def subscribe(self, callback: Callable[[Event], Any],
                  event_types: Optional[Iterable[Type[Event]]] = None) -> SubscriptionToken:
        return self._events.subscribe(callback, event_types)

    def unsubscribe(self, token: SubscriptionToken) -> None:
        self._events.unsubscribe(token)

    def add_schedule(self, schedule: Schedule, conflict_policy: ConflictPolicy) -> None:
        event: Event
        serialized_data = self.serializer.serialize(schedule)
        insert = self.t_schedules.insert().\
            values(id=schedule.id, task_id=schedule.task_id, serialized_data=serialized_data,
                   next_fire_time=schedule.next_fire_time)
        try:
            with self.engine.begin() as conn:
                conn.execute(insert)
                event = ScheduleAdded(schedule_id=schedule.id,
                                      next_fire_time=schedule.next_fire_time)
                self._events.publish(event)
        except IntegrityError:
            if conflict_policy is ConflictPolicy.exception:
                raise ConflictingIdError(schedule.id) from None
            elif conflict_policy is ConflictPolicy.replace:
                update = self.t_schedules.update().\
                    where(self.t_schedules.c.id == schedule.id).\
                    values(serialized_data=serialized_data,
                           next_fire_time=schedule.next_fire_time)
                with self.engine.begin() as conn:
                    conn.execute(update)

                event = ScheduleUpdated(schedule_id=schedule.id,
                                        next_fire_time=schedule.next_fire_time)
                self._events.publish(event)

    def remove_schedules(self, ids: Iterable[str]) -> None:
        with self.engine.begin() as conn:
            delete = self.t_schedules.delete().where(self.t_schedules.c.id.in_(ids))
            if self._supports_update_returning:
                delete = delete.returning(self.t_schedules.c.id)
                removed_ids: Iterable[str] = [row[0] for row in conn.execute(delete)]
            else:
                # TODO: actually check which rows were deleted?
                conn.execute(delete)
                removed_ids = ids

        for schedule_id in removed_ids:
            self._events.publish(ScheduleRemoved(schedule_id=schedule_id))

    def get_schedules(self, ids: Optional[Set[str]] = None) -> List[Schedule]:
        query = select([self.t_schedules.c.id, self.t_schedules.c.serialized_data]).\
            order_by(self.t_schedules.c.id)
        if ids:
            query = query.where(self.t_schedules.c.id.in_(ids))

        with self.engine.begin() as conn:
            result = conn.execute(query)
            return self._deserialize_schedules(result)

    def acquire_schedules(self, scheduler_id: str, limit: int) -> List[Schedule]:
        with self.engine.begin() as conn:
            now = datetime.now(timezone.utc)
            acquired_until = now + timedelta(seconds=self.lock_expiration_delay)
            schedules_cte = select(self.t_schedules.c.id).\
                where(and_(self.t_schedules.c.next_fire_time.isnot(None),
                           self.t_schedules.c.next_fire_time <= now,
                           or_(self.t_schedules.c.acquired_until.is_(None),
                               self.t_schedules.c.acquired_until < now))).\
                order_by(self.t_schedules.c.next_fire_time).\
                limit(limit).cte()
            subselect = select([schedules_cte.c.id])
            update = self.t_schedules.update().\
                where(self.t_schedules.c.id.in_(subselect)).\
                values(acquired_by=scheduler_id, acquired_until=acquired_until)
            if self._supports_update_returning:
                update = update.returning(self.t_schedules.c.id,
                                          self.t_schedules.c.serialized_data)
                result = conn.execute(update)
            else:
                conn.execute(update)
                query = select([self.t_schedules.c.id, self.t_schedules.c.serialized_data]).\
                    where(and_(self.t_schedules.c.acquired_by == scheduler_id))
                result = conn.execute(query)

            schedules = self._deserialize_schedules(result)

        return schedules

    def release_schedules(self, scheduler_id: str, schedules: List[Schedule]) -> None:
        with self.engine.begin() as conn:
            update_events: List[ScheduleUpdated] = []
            finished_schedule_ids: List[str] = []
            update_args: List[Dict[str, Any]] = []
            for schedule in schedules:
                if schedule.next_fire_time is not None:
                    try:
                        serialized_data = self.serializer.serialize(schedule)
                    except SerializationError:
                        self._logger.exception('Error serializing schedule %r – '
                                               'removing from data store', schedule.id)
                        finished_schedule_ids.append(schedule.id)
                        continue

                    update_args.append({
                        'p_id': schedule.id,
                        'p_serialized_data': serialized_data,
                        'p_next_fire_time': schedule.next_fire_time
                    })
                else:
                    finished_schedule_ids.append(schedule.id)

            # Update schedules that have a next fire time
            if update_args:
                p_id: BindParameter = bindparam('p_id')
                p_serialized: BindParameter = bindparam('p_serialized_data')
                p_next_fire_time: BindParameter = bindparam('p_next_fire_time')
                update = self.t_schedules.update().\
                    where(and_(self.t_schedules.c.id == p_id,
                               self.t_schedules.c.acquired_by == scheduler_id)).\
                    values(serialized_data=p_serialized, next_fire_time=p_next_fire_time,
                           acquired_by=None, acquired_until=None)
                next_fire_times = {arg['p_id']: arg['p_next_fire_time'] for arg in update_args}
                if self._supports_update_returning:
                    update = update.returning(self.t_schedules.c.id)
                    updated_ids = [row[0] for row in conn.execute(update, update_args)]
                else:
                    # TODO: actually check which rows were updated?
                    conn.execute(update, update_args)
                    updated_ids = list(next_fire_times)

                for schedule_id in updated_ids:
                    event = ScheduleUpdated(schedule_id=schedule_id,
                                            next_fire_time=next_fire_times[schedule_id])
                    update_events.append(event)

            # Remove schedules that have no next fire time or failed to serialize
            if finished_schedule_ids:
                delete = self.t_schedules.delete().\
                    where(self.t_schedules.c.id.in_(finished_schedule_ids))
                conn.execute(delete)

        for event in update_events:
            self._events.publish(event)

        for schedule_id in finished_schedule_ids:
            self._events.publish(ScheduleRemoved(schedule_id=schedule_id))

    def get_next_schedule_run_time(self) -> Optional[datetime]:
        query = select(self.t_schedules.c.id).\
            where(self.t_schedules.c.next_fire_time.isnot(None)).\
            order_by(self.t_schedules.c.next_fire_time).\
            limit(1)
        with self.engine.begin() as conn:
            result = conn.execute(query)
            return result.scalar()

    def add_job(self, job: Job) -> None:
        now = datetime.now(timezone.utc)
        serialized_data = self.serializer.serialize(job)
        insert = self.t_jobs.insert().values(id=job.id.hex, task_id=job.task_id,
                                             created_at=now, serialized_data=serialized_data)
        with self.engine.begin() as conn:
            conn.execute(insert)

        event = JobAdded(job_id=job.id, task_id=job.task_id, schedule_id=job.schedule_id,
                         tags=job.tags)
        self._events.publish(event)

    def get_jobs(self, ids: Optional[Iterable[UUID]] = None) -> List[Job]:
        query = select([self.t_jobs.c.id, self.t_jobs.c.serialized_data]).\
            order_by(self.t_jobs.c.id)
        if ids:
            job_ids = [job_id.hex for job_id in ids]
            query = query.where(self.t_jobs.c.id.in_(job_ids))

        with self.engine.begin() as conn:
            result = conn.execute(query)
            return self._deserialize_jobs(result)

    def acquire_jobs(self, worker_id: str, limit: Optional[int] = None) -> List[Job]:
        with self.engine.begin() as conn:
            now = datetime.now(timezone.utc)
            acquired_until = now + timedelta(seconds=self.lock_expiration_delay)
            query = select([self.t_jobs.c.id, self.t_jobs.c.serialized_data]).\
                where(or_(self.t_jobs.c.acquired_until.is_(None),
                          self.t_jobs.c.acquired_until < now)).\
                order_by(self.t_jobs.c.created_at).\
                limit(limit)

            serialized_jobs: Dict[str, bytes] = {row[0]: row[1]
                                                 for row in conn.execute(query)}
            if serialized_jobs:
                update = self.t_jobs.update().\
                    values(acquired_by=worker_id, acquired_until=acquired_until).\
                    where(self.t_jobs.c.id.in_(serialized_jobs))
                conn.execute(update)

            return self._deserialize_jobs(serialized_jobs.items())

    def release_job(self, worker_id: str, job_id: UUID, result: Optional[JobResult]) -> None:
        with self.engine.begin() as conn:
            now = datetime.now(timezone.utc)
            serialized_result = self.serializer.serialize(result)
            insert = self.t_job_results.insert().\
                values(job_id=job_id.hex, finished_at=now, serialized_data=serialized_result)
            conn.execute(insert)

            delete = self.t_jobs.delete().where(self.t_jobs.c.id == job_id.hex)
            conn.execute(delete)

    def get_job_result(self, job_id: UUID) -> Optional[JobResult]:
        with self.engine.begin() as conn:
            query = select(self.t_job_results.c.serialized_data).\
                where(self.t_job_results.c.job_id == job_id.hex)
            result = conn.execute(query)

            delete = self.t_job_results.delete().\
                where(self.t_job_results.c.job_id == job_id.hex)
            conn.execute(delete)

            serialized_result = result.scalar()
            return self.serializer.deserialize(serialized_result) if serialized_result else None