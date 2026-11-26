from dataclasses import dataclass

ARCHIVE_VERSION = 'shift_archive.20261126.v1'

# 回看计分的处置状态：同版冲突单退出计分，其余单保留原派单。
SCORING_EXCLUDED = '退出计分'
# 只有右截尾记录的单：等待证实取件，暂不纳入误差。
PENDING_PICKUP = '待证实取件'
# 班结后才到站的单据：只进余时评估，不回写旧班预测与派单。
LATE_DOCUMENT = '班结后到站'
CONFLICT_DOCUMENT = '同版实绩冲突'
SETTLED_DOCUMENT = '已证实取件'


@dataclass(frozen=True, slots=True)
class ArchivedTicket:
    """班结时刻冻结的一张单：余时、派单和当时已留架时长均不可变。"""
    key: str
    cohort: str
    estimate: float
    lower: float | None
    upper: float | None
    state: str
    reason: str
    cost: int
    held_age: float
    rule_hits: tuple[str, ...]
    measurements: dict
    unmeasured: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LateArrival:
    """班结后到站的核实记录，按单号保留全部到站版次。"""
    key: str
    cohort: str
    received: int
    revision: int
    result_day: int | None
    outcome: float | None
    observed: bool
    held_age: float
    status: str
    note: str


@dataclass(frozen=True, slots=True)
class TicketReview:
    """逐单回看结论。计分以班结当时已留架时长为基准。"""
    key: str
    archived_state: str
    scoring_status: str
    held_age_at_close: float
    estimate: float
    actual_remaining: float | None
    signed_error: float | None
    absolute_error: float | None
    basis: str
    note: str


@dataclass(frozen=True, slots=True)
class ShiftArchive:
    """一次班结的不可变快照，旧班预测和派单始终可据此复现。"""
    archive_stamp: str
    closed_at: int
    batch_hash: str
    policy_hash: str
    run_id: str
    capacity: int
    mode: str
    cutoff: int
    tickets: tuple[ArchivedTicket, ...]
    late: tuple[LateArrival, ...]
    excluded_conflicts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReplayScore:
    """回看汇总；分母只含已证实取件且未退出计分的单。"""
    status: str
    scored: int
    excluded_conflict: int
    awaiting_pickup: int
    late_arrivals: int
    mae: float | None
    rmse: float | None
    bias: float | None
    rows: tuple[TicketReview, ...]
