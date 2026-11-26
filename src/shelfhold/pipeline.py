from dataclasses import replace
import json
from pathlib import Path

from .hold_slips import service as intake_service
from .patron_cutoffs import service as cohort_service
from .pickup_context import service as feature_service
from .shelf_survival import service as model_service
from .tail_replay import service as calibration_service
from .reading_mix import service as monitor_service
from .release_review import service as dispatch_service
from .hold_catalogue import service as audit_service
from .shift_archive import service as archive_service
from .shift_archive import intake as archive_intake
from .patron_cutoffs import contracts as window_contract

PRESET = Path(__file__).resolve().parents[2] / 'sample_data' / 'hold_slips_sample.json'
MODULES = (('hold_slips', '预约回执'),
 ('patron_cutoffs', '取件分窗'),
 ('pickup_context', '到馆因素'),
 ('shelf_survival', '留架生存'),
 ('tail_replay', '余时回放'),
 ('reading_mix', '借阅分组'),
 ('release_review', '释放复核'),
 ('hold_catalogue', '留架清册'),
 ('shift_archive', '班结回看'))
DISPLAY = {'title': '图书预约留架册',
 'object_name': '预约单',
 'unit': '小时',
 'role': '借阅台当班员',
 'endpoint': 'shelfhold',
 'identity': 'RESERVES',
 'location': '流通部 · 预约保留区',
 'visual': '留架余时与保留线',
 'queue': '等待取件登记',
 'resource': '馆员复核位',
 'action': '安排复核释放',
 'lane': 'L3',
 'field_title': '留架回访安排'}


def open_fixture(path=PRESET):
    document = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(document, dict) or not isinstance(document.get('records'), list):
        raise ValueError('样例必须包含 records 数组')
    if len(document['records']) > 2500:
        raise ValueError('单次导入最多处理 2500 条单据')
    return document


def stage_at(index, details, records, issues=()):
    code, title = MODULES[index]
    return {'key': code, 'title': title, 'summary': details, 'records': list(records), 'errors': list(issues)}


def rows_for(plan, scored, vectors):
    by_key = {item.key: item for item in scored}
    factor_names = ('到馆时间成本', '周末标记', '当前延期负担', '预约等待小时积', '通知后已过去')
    order = []
    for job in plan.jobs:
        point = by_key[job.key]
        if len(point.contributions) == len(factor_names):
            reasons = sorted(zip(factor_names, point.contributions, strict=True), key=lambda val: (-abs(val[1]), val[0]))
            driver = ' · '.join(f'{key} {amount:+.2f}' for key, amount in reasons[:2])
        else:
            driver = ' · '.join(f'{amount:+.2f}' for amount in point.contributions)
        interval = ('–' if job.lower is None else f'{job.lower:.2f}–{job.upper:.2f}')
        order.append({'key': job.key, 'zone': job.cohort, 'estimate': job.estimate, 'interval': interval,
                      'state': job.state, 'cost': job.cost, 'reason': job.reason, 'evidence': job.summary,
                      'drivers': driver, 'missing': len(point.missing), 'hits': list(job.rule_hits),
                      'measurements': dict(zip(('notice_lag', 'walk_mins', 'weekday', 'renewals', 'waiting_readers', 'held_age'), point.input_values, strict=True)),
                      'unmeasured': list(point.missing), 'operations': feature_service.business_factors(vectors[job.key])})
    return order


def comparison_rows(predictions):
    return [{'单号': point.key, '分组': point.cohort, '原始估计': round(point.score, 4),
             '补值字段': len(point.missing), '偏离特征': '、'.join(point.excursions) or '—'}
            for point in predictions[:14]]


def replay_metrics(report):
    result = [{'label': '回放实绩样本', 'value': report.count}, {'label': '回放绝对误差', 'value': report.mae}]
    return result


def execute(documents, capacity=12, mode='balanced', *, as_of=96):
    if type(as_of) is not int or not 91 <= as_of <= 3650:
        raise ValueError('请输入 91 至 3650 之间的整数工日')
    if type(documents) not in (list,tuple):
        raise ValueError('提交对象应为行业单据序列')
    if not documents or len(documents)>2500:
        raise ValueError('单次提交限 1–2500 张单据')
    intake = intake_service.load_samples(iter(documents),as_of)
    timeline=window_contract.WindowRule.standard(allow_censor=True)
    windows=cohort_service.build_windows(intake.entries,replace(timeline,as_of=as_of))
    cohort_service.model_inputs(windows)
    state = feature_service.fit_features(windows.train)
    features = feature_service.transform_windows(windows,state)
    apply_stamp = state.fingerprint
    kernel = model_service.fit_model(features.train,apply_stamp)
    evaluator = lambda partition: model_service.predict(kernel,partition,apply_stamp)
    # Archive, replay and current work all use a frozen estimator.
    fitted_calibration = calibration_service.prepare_verification(evaluator(features.calibration))
    grader = lambda forum: calibration_service.validate_predictions(forum,fitted_calibration)
    evaluated=grader(evaluator(features.replay))
    scored=evaluator(features.live)
    coverage=monitor_service.coverage_by_group(features.calibration,features.live)
    baseline = monitor_service.fit_baseline(features.train,apply_stamp)
    drift = monitor_service.scan_current(baseline,features.live,apply_stamp)
    checked_live=grader(scored)
    allocation = dispatch_service.plan_jobs(checked_live,drift.domain,capacity,mode)
    closed = audit_service.close_batch(tuple(documents),intake,windows,allocation,apply_stamp,kernel.name,mode)
    performance = calibration_service.performance(evaluated)
    states = dict(closed.totals.states)
    overview = [
        {'label': '当班预约单', 'value': len(allocation.jobs)},
        {'label': '已分派', 'value': states.get('已分派', 0)},
        {'label': '候补', 'value': states.get('候补', 0)},
        {'label': '暂停及人工判读', 'value': states.get('暂停', 0) + states.get('人工判读', 0)},
        {'label': '额度结余', 'value': allocation.balance},
    ]
    vectors = {item.key: item for item in features.live}
    row_list = rows_for(allocation, scored, vectors)
    operating_rows = dispatch_service.operating_ledger(row_list)
    overtime = calibration_service.boundary_diagnostics(evaluated, 34)
    sector = calibration_service.sector_replay(evaluated, 34)
    operating_shift = monitor_service.shop_floor_shift(features.train, features.live)
    field_evidence = audit_service.sign_field_evidence(row_list, allocation)
    # 班结快照：旧班预测与派单冻结；晚到单据只登记，不回写任何当班结论。
    _on_time, late_arrivals, conflict_keys, _late_issues = archive_intake.collect(documents, as_of)
    shift_archive = archive_service.build_archive(
        row_list, late_arrivals, conflict_keys, closed_at=as_of, cutoff=as_of,
        batch_hash=closed.batch_hash, policy_hash=closed.policy_hash, run_id=closed.stamp,
        capacity=capacity, mode=mode)
    shift_review = archive_service.settle_shift(shift_archive)
    review_summary, review_detail = archive_service.review_evidence(shift_review, shift_archive)
    features_glance = feature_service.feature_evidence(state) + feature_service.measure_review(features)
    weight_rows = [{'量测': key, '取值': value} for key, value in kernel.evidence]
    ignored = [issue.evidence() for issue in intake.issues]
    details = [
        stage_at(0, f'接收 {intake.source_rows} 条，采用 {len(intake.entries)} 条', intake_service.preliminary_evidence(intake), ignored),
        stage_at(1, f'拟合 {len(windows.train)} 条，校准 {len(windows.calibration)} 条，回放 {len(windows.replay)} 条', cohort_service.window_evidence(windows) + cohort_service.readiness_evidence(windows, as_of)),
        stage_at(2, '特征转换版本 ' + state.fingerprint, features_glance),
        stage_at(3, kernel.name, weight_rows + model_service.inspection_rows(kernel, features) + comparison_rows(scored)),
        stage_at(4, f'{fitted_calibration.method} · {performance.status} · {performance.count} 条回放实绩', calibration_service.calibration_evidence(performance) + [overtime] + sector),
        stage_at(5, f'{drift.status} · {len(drift.domain)} 张单据需外推判读', monitor_service.shift_evidence(drift, coverage) + operating_shift),
        stage_at(6, f'本班占用 {allocation.capacity-allocation.balance} / {allocation.capacity} 个工位单位', dispatch_service.action_evidence(allocation) + dispatch_service.operating_group(checked_live, allocation)),
        stage_at(7, f'处置凭证 {closed.stamp} · 额度守恒', audit_service.reconciliation_evidence(closed) + field_evidence['执行业务单']),
    ]
    details[6]['operating_rows'] = operating_rows
    details[6]['operating_title'] = '留架回访安排'
    # 旧班回看：逐单核对余时、派单与事后计分依据，晚到记录不改动冻结快照。
    details.append(stage_at(8, f'班结归档 {shift_archive.archive_stamp} · 已计分 {shift_review.scored} 单',
                            review_summary + [{'核对项': '晚到登记', '数量/取值': shift_review.late_arrivals}],
                            archive_service.late_evidence(shift_archive)))
    details[8]['review_rows'] = review_detail
    details[8]['review_title'] = '旧班逐单回看'
    points = [{'label': row['key'], 'value': row['estimate'], 'state': row['state'], 'zone': row['zone'],
               'upper': row['interval'], 'cost': row['cost']} for row in sorted(row_list, key=lambda row: row['key'])]
    def estimate(vector):
        return calibration_service.check_point(model_service.predict_one(kernel, vector), fitted_calibration).estimate

    # Compare interventions against frozen features, model and calibration;
    # scenario estimates never overwrite the base ticket or its decision.
    for row in row_list:
        current = vectors[row['key']]
        row['alternatives'] = feature_service.render_interventions(current, state, estimate)
        row['joint_experiments'] = feature_service.render_joint_experiments(current,state,estimate)
    details[7]['records'].extend(audit_service.site_checklist(row_list))
    details[7]['records'].extend(audit_service.intervention_summary(row_list))
    overview_report = {'capacity' : capacity, 'mode': mode, 'as_of': as_of, 'sample_hash': closed.batch_hash,
              'policy_hash': closed.policy_hash, 'feature_stamp': state.fingerprint,
              'model': kernel.name, 'run_id': closed.stamp, 'object_name': DISPLAY['object_name'],
              'unit': DISPLAY['unit'], 'sections': details, 'flow_counts': list(states.items()),
 'shift_archive': {'archive_stamp': shift_archive.archive_stamp, 'closed_at': shift_archive.closed_at,
                   'cutoff': shift_archive.cutoff, 'run_id': shift_archive.run_id,
                   'scoring': {'status': shift_review.status, 'scored': shift_review.scored,
                               'excluded_conflict': shift_review.excluded_conflict,
                               'awaiting_pickup': shift_review.awaiting_pickup,
                               'late_arrivals': shift_review.late_arrivals,
                               'mae': shift_review.mae, 'rmse': shift_review.rmse, 'bias': shift_review.bias}},
              'measures': {'notice_lag': ('通知送达延迟', 'h'),
 'walk_mins': ('取件步行估计', 'min'),
 'weekday': ('通知星期', '日'),
 'renewals': ('延期申请', '次'),
 'waiting_readers': ('后续预约户', '户'),
 'held_age': ('已留架时间', 'h')}, 'metrics': overview, 'performance': replay_metrics(performance), 'rows': row_list, 'series': points}
    return overview_report


def preview(capacity=12, mode='balanced'):
    fixture = open_fixture()
    return execute(fixture['records'], capacity, mode, as_of=fixture['as_of'])
