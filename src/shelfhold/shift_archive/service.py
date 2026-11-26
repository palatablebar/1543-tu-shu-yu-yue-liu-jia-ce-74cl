from hashlib import sha256
from math import sqrt
from statistics import mean

from ..hold_catalogue.service import canonical_json
from .contracts import (ARCHIVE_VERSION, ArchivedTicket, ReplayScore,
                        ShiftArchive, TicketReview, PENDING_PICKUP, SCORING_EXCLUDED,
                        SETTLED_DOCUMENT)


def archive_ticket(row):
    """从清册展示行冻结一张班结单。"""
    measurements = dict(row['measurements'])
    held_age = measurements['held_age']
    return ArchivedTicket(
        key=row['key'], cohort=row['zone'], estimate=float(row['estimate']),
        lower=None if row['interval'] == '–' else _parse_lower(row['interval']),
        upper=None if row['interval'] == '–' else _parse_upper(row['interval']),
        state=row['state'], reason=row['reason'], cost=int(row['cost']),
        held_age=float(held_age), rule_hits=tuple(row['hits']),
        measurements=measurements, unmeasured=tuple(row['unmeasured']))


def _parse_lower(interval):
    return float(interval.split('–')[0])


def _parse_upper(interval):
    return float(interval.split('–')[1])


def archive_fingerprint(closed_at, cutoff, tickets, late, conflicts):
    payload = (ARCHIVE_VERSION, closed_at, cutoff,
               tuple(sorted((t.key, t.state, t.reason, t.cost, t.estimate, t.held_age) for t in tickets)),
               tuple(sorted((a.key, a.received, a.revision, a.outcome, a.observed) for a in late)),
               tuple(sorted(conflicts)))
    return sha256(canonical_json(payload).encode()).hexdigest()[:16]


def build_archive(rows, late, conflict_keys, *, closed_at, cutoff, batch_hash, policy_hash,
                  run_id, capacity, mode):
    """班结后固化快照。冲突单保留原派单，只在回看时退出计分。"""
    tickets = tuple(archive_ticket(row) for row in sorted(rows, key=lambda item: item['key']))
    stamp = archive_fingerprint(closed_at, cutoff, tickets, late, tuple(conflict_keys))
    return ShiftArchive(stamp, closed_at, batch_hash, policy_hash, run_id, capacity, mode, cutoff,
                        tickets, tuple(late), tuple(sorted(conflict_keys)))


def _actual_remaining(outcome, held_age):
    """实绩余时 = 总留架时长 − 班结当时已留架时长；计分基准不随晚到记录移动。"""
    if outcome is None:
        return None
    return max(0.0, outcome - held_age)


def review_ticket(ticket, late_index):
    """逐单核对当时余时、派单与事后计分依据。"""
    late = late_index.get(ticket.key)
    if late and any(a.status == '同版实绩冲突' for a in late):
        # 同版实绩冲突：这张单退出计分，原派单照留。
        return TicketReview(ticket.key, ticket.state, SCORING_EXCLUDED, ticket.held_age,
                            ticket.estimate, None, None, None,
                            '同版实绩冲突，该单退出计分，原派单照留',
                            '保留班结派单：' + ticket.reason)
    if late:
        chosen = max(late, key=lambda arrival: (arrival.revision, arrival.received))
        actual = _actual_remaining(chosen.outcome, ticket.held_age)
        if chosen.observed is False or chosen.outcome is None:
            # 仍只有截尾记录：等证实取件再计入误差。
            return TicketReview(ticket.key, ticket.state, PENDING_PICKUP, ticket.held_age,
                                ticket.estimate, actual, None, None,
                                '仅有右截尾记录，待证实取件后再计分',
                                f'到站工日 {chosen.received}，版次 {chosen.revision}')
        return _scored(ticket, actual, SETTLED_DOCUMENT,
                       f'晚到记录已核实（到站 {chosen.received}，版次 {chosen.revision}），余时按班结已留架 {ticket.held_age:g} 小时折算')
    return TicketReview(ticket.key, ticket.state, PENDING_PICKUP, ticket.held_age,
                        ticket.estimate, None, None, None,
                        '尚未收到事后记录，等待证实取件',
                        '派单与余时维持班结快照')


def _scored(ticket, actual, status, note):
    error = ticket.estimate - actual
    return TicketReview(ticket.key, ticket.state, status, ticket.held_age, ticket.estimate,
                        round(actual, 4), round(error, 4), round(abs(error), 4),
                        '以班结当时已留架时长为基准核对余时', note)


def settle_shift(archive):
    """回看整班：晚到核实记录评估余时，旧班预测与派单不变。"""
    late_index = {}
    for arrival in archive.late:
        late_index.setdefault(arrival.key, []).append(arrival)
    rows = tuple(review_ticket(ticket, late_index) for ticket in archive.tickets)
    scored = [row for row in rows if row.scoring_status == SETTLED_DOCUMENT]
    conflicts = sum(row.scoring_status == SCORING_EXCLUDED for row in rows)
    awaiting = sum(row.scoring_status == PENDING_PICKUP for row in rows)
    if not scored:
        metrics = (None, None, None)
        status = 'NO_SETTLED_PICKUP'
    else:
        errors = [row.signed_error for row in scored]
        metrics = (round(mean(map(abs, errors)), 4),
                   round(sqrt(mean(value ** 2 for value in errors)), 4),
                   round(mean(errors), 4))
        status = 'READY'
    return ReplayScore(status, len(scored), conflicts, awaiting, len(archive.late), *metrics, rows)


def review_evidence(score, archive):
    """清册页回看证据表。"""
    summary = [
        {'核对项': '班结凭证', '数量/取值': archive.archive_stamp},
        {'核对项': '班结工日', '数量/取值': archive.closed_at},
        {'核对项': '已计分（证实取件）', '数量/取值': score.scored},
        {'核对项': '退出计分（同版冲突）', '数量/取值': score.excluded_conflict},
        {'核对项': '待证实取件（含截尾）', '数量/取值': score.awaiting_pickup},
        {'核对项': '班结后到站单据', '数量/取值': score.late_arrivals},
        {'核对项': '回看平均绝对误差', '数量/取值': '—' if score.mae is None else score.mae},
        {'核对项': '回看均方根误差', '数量/取值': '—' if score.rmse is None else score.rmse},
        {'核对项': '回看偏差（估−实）', '数量/取值': '—' if score.bias is None else score.bias},
    ]
    detail = [{'单号': row.key, '班结处置': row.archived_state, '计分状态': row.scoring_status,
               '班结已留架(h)': row.held_age_at_close, '当时余时估计': row.estimate,
               '实绩余时': '—' if row.actual_remaining is None else row.actual_remaining,
               '绝对误差': '—' if row.absolute_error is None else row.absolute_error,
               '计分依据': row.basis, '备注': row.note} for row in score.rows]
    return summary, detail


def late_evidence(archive):
    return [{'单号': item.key, '作业区': item.cohort, '到站工日': item.received, '版次': item.revision,
             '总留架实绩': '—' if item.outcome is None else item.outcome,
             '截尾': '是' if item.observed is False else '否', '核实状态': item.status, '说明': item.note}
            for item in archive.late]
