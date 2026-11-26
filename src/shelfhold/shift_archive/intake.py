from collections import defaultdict

from ..hold_slips.contracts import Issue, MeasureIssue
from ..hold_slips.service import read_assay
from .contracts import CONFLICT_DOCUMENT, LATE_DOCUMENT, LateArrival


def parse_all(records):
    """解析整批单据，保留无法解析行的隔离原因。"""
    decoded, issues = [], {}
    for line, raw in enumerate(records, 1):
        try:
            decoded.append(read_assay(raw))
        except MeasureIssue as problem:
            name = raw.get('key', '') if isinstance(raw, dict) else ''
            issues[line] = Issue(line, str(name)[:64], problem.field, problem.code)
    return decoded, tuple(issues[line] for line in sorted(issues))


def conflicting_revisions(records):
    """同版（同单号、同版次）出现不同测量结果时，该版次标记冲突。

    与 hold_slips 保持一致：按内容签名判定，且结论与到站顺序无关。
    返回冲突的 (单号, 版次) 集合。
    """
    signatures = defaultdict(set)
    for row in records:
        signatures[(row.key, row.revision)].add(row.signature())
    return frozenset(pair for pair, seen in signatures.items() if len(seen) > 1)


def split_late(records, cutoff):
    """班结后到站（received 晚于截止日）的单据不参与旧班预测。

    训练生存模型与校准区间各自的窗口截止日在分窗阶段另行守住，
    这里只负责把晚到记录隔离出旧班快照，供核实后评估余时。
    """
    late, on_time = [], []
    for row in records:
        (late if row.received > cutoff else on_time).append(row)
    return tuple(on_time), tuple(late)


def verify_late(late_rows, conflict_keys):
    """晚到记录核实后登记；同版冲突单标注冲突，仍不回写旧班。"""
    arrivals = []
    for row in sorted(late_rows, key=lambda item: (item.received, item.key, item.revision)):
        if (row.key, row.revision) in conflict_keys:
            status, note = CONFLICT_DOCUMENT, '同版实绩冲突，晚到记录仅登记不参与评估'
        else:
            status, note = LATE_DOCUMENT, '班结后到站，核实后只用于余时评估'
        arrivals.append(LateArrival(row.key, row.cohort, row.received, row.revision,
                                    row.result_day, row.outcome, row.observed,
                                    row.held_age if row.held_age is not None else 0.0,
                                    status, note))
    return tuple(arrivals)


def collect(records, cutoff):
    """整批分流：可用旧班单、晚到登记、冲突单号与解析隔离行。"""
    if type(cutoff) is not int or cutoff not in range(3651):
        raise ValueError('班结日应为 0–3650 之间的整数')
    decoded, issues = parse_all(records)
    # 冲突判定在截止日过滤之前，晚到段内部的同版冲突同样可识别。
    conflict_pairs = conflicting_revisions(decoded)
    on_time, late = split_late(decoded, cutoff)
    return on_time, verify_late(late, conflict_pairs), frozenset(key for key, _ in conflict_pairs), issues
