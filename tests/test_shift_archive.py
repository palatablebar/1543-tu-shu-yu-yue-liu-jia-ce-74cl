import pytest

from shelfhold.pipeline import open_fixture, preview, execute
from shelfhold.shift_archive.intake import collect, conflicting_revisions
from shelfhold.shift_archive.service import build_archive, settle_shift
from shelfhold.hold_slips.service import read_assay


@pytest.fixture
def live_ticket(observation):
    # 样例中的当班单（day>=88），用于构造班结后到站记录。
    sample = open_fixture()
    return next(dict(row) for row in sample['records'] if row['day'] >= 88)


def _late_pickup(ticket, *, revision=2, outcome=60.0, observed=True, result_day=102):
    return {**ticket, 'revision': revision, 'received': 100,
            'result_day': None if result_day is None else result_day,
            'outcome': None if observed is False else outcome, 'observed': observed}


def test_archive_stage_is_part_of_review_sections():
    report = preview()
    assert len(report['sections']) == 9
    stage = report['sections'][8]
    assert stage['key'] == 'shift_archive'
    assert stage['review_title'] == '旧班逐单回看'
    assert {row['单号'] for row in stage['review_rows']} == {row['key'] for row in report['rows']}


def test_late_records_are_isolated_from_old_shift_cutoff(live_ticket):
    late = _late_pickup(live_ticket)
    on_time, late_rows, conflicts, issues = collect([live_ticket, late], 96)
    assert all(row.received <= 96 for row in on_time)
    assert [row.key for row in late_rows] == [live_ticket['key']]
    assert conflicts == frozenset() and issues == ()


def test_late_pickup_is_scored_against_held_age_at_close(live_ticket):
    late = _late_pickup(live_ticket, outcome=60.0)
    docs = [dict(r) for r in open_fixture()['records']] + [late]
    report = execute(docs)
    rows = {row['单号']: row for row in report['sections'][8]['review_rows']}
    view = rows[live_ticket['key']]
    held_age = live_ticket['held_age']
    assert view['计分状态'] == '已证实取件'
    assert view['班结已留架(h)'] == held_age
    # 实绩余时 = 总留架时长 − 班结当时已留架时长，基准不随晚到记录移动。
    assert view['实绩余时'] == round(60.0 - held_age, 4)
    assert view['绝对误差'] == pytest.approx(abs(view['当时余时估计'] - view['实绩余时']))


def test_censored_late_record_waits_for_confirmed_pickup(live_ticket):
    late = _late_pickup(live_ticket, observed=False, result_day=None)
    report = execute([dict(r) for r in open_fixture()['records']] + [late])
    rows = {row['单号']: row for row in report['sections'][8]['review_rows']}
    view = rows[live_ticket['key']]
    assert view['计分状态'] == '待证实取件'
    assert view['绝对误差'] == '—' and view['实绩余时'] == '—'
    assert report['shift_archive']['scoring']['awaiting_pickup'] >= 1


def test_same_version_conflict_exits_scoring_but_keeps_dispatch(live_ticket):
    first = {**live_ticket, 'revision': 4, 'received': 99, 'result_day': 103,
             'outcome': 70.0, 'observed': True}
    second = {**first, 'notice_lag': live_ticket['notice_lag'] + 1}
    report = execute([dict(r) for r in open_fixture()['records']] + [first, second])
    rows = {row['单号']: row for row in report['sections'][8]['review_rows']}
    view = rows[live_ticket['key']]
    assert view['计分状态'] == '退出计分'
    # 原派单照留：班结处置与无晚到记录时一致。
    baseline = {row['key']: row['state'] for row in preview()['rows']}
    current = {row['key']: row['state'] for row in report['rows']}
    assert view['班结处置'] == current[live_ticket['key']] == baseline[live_ticket['key']]
    assert report['shift_archive']['scoring']['excluded_conflict'] == 1


def test_conflict_detection_is_order_independent(observation):
    base = read_assay(observation)
    alt = read_assay({**observation, 'notice_lag': observation['notice_lag'] + 0.5})
    pair = conflicting_revisions((base, alt))
    assert pair == frozenset({(base.key, base.revision)})
    assert conflicting_revisions((alt, base)) == pair


def test_late_arrivals_never_move_old_predictions_or_dispatch(live_ticket):
    late = _late_pickup(live_ticket, outcome=80.0)
    before = preview()
    after = execute([dict(r) for r in open_fixture()['records']] + [late])
    assert after['feature_stamp'] == before['feature_stamp']
    assert after['rows'] == before['rows']
    # 晚到单据只登记在归档层，不进入当班清册。
    assert after['shift_archive']['scoring']['late_arrivals'] >= 1
    late_keys = {item['单号'] for item in after['sections'][8]['errors']}
    assert live_ticket['key'] in late_keys


def test_archive_is_reproducible_and_frozen(live_ticket):
    late = _late_pickup(live_ticket)
    docs = [dict(r) for r in open_fixture()['records']] + [late]
    first = execute(docs)
    second = execute(list(reversed(docs)))
    assert first['run_id'] == second['run_id']
    assert first['shift_archive'] == second['shift_archive']
    # 归档摘要与逐单行数守恒。
    scoring = first['shift_archive']['scoring']
    stage = first['sections'][8]
    assert scoring['scored'] + scoring['excluded_conflict'] + scoring['awaiting_pickup'] == len(stage['review_rows'])


def test_review_metrics_undefined_without_settled_pickup():
    report = preview()
    archive = build_archive(report['rows'], (), (), closed_at=96, cutoff=96,
                            batch_hash='b', policy_hash='p', run_id='r',
                            capacity=12, mode='balanced')
    score = settle_shift(archive)
    assert score.status == 'NO_SETTLED_PICKUP'
    assert score.scored == 0 and score.mae is None and score.rmse is None
    assert all(row.scoring_status == '待证实取件' for row in score.rows)
