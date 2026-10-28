"""Cancel the automatic midnight round before scheduler selection or claim.

Legacy rotation stores its phase in total_returngift/total_alliedteam_battle,
but no call origin. Missing manual provenance defaults to the automatic phase
in rotation mode; explicit manual calls and rotation-disabled mode are retained.
"""
from datetime import datetime, timedelta

from tasks.MultiDailyAltAcc.task_plan import TASK_KEYS, load_task_plan

MANUAL_SCHEDULE_FIELD = 'single_purpose_schedule_manual'


def is_automatic_midnight_phase(config):
    get = config.get if isinstance(config, dict) else lambda key, default: getattr(config, key, default)
    return (not get('disable_task_rotation', False)
            and not get(MANUAL_SCHEDULE_FIELD, False)
            and (get('total_returngift_enable', False)
                 or get('total_alliedteam_battle_enable', False)))


def is_disabled_automatic_midnight(task):
    """Plan-independent execution guard, including when the plan is invalid."""
    config = task.get('multi_daily_alt_acc_config', {})
    return (task.get('scheduler', {}).get('enable', False)
            and not config.get('team_battle_midnight_schedule_enable', True)
            and is_automatic_midnight_phase(config))


def cancel_disabled_midnight_schedule(canonical, now=None, plan=None, normal_round=None):
    """Mutate only a disabled MultiDaily midnight phase; return changed paths.

    The Store calls this while holding the existing lifecycle lock. No device,
    timer or runtime task object is needed, including when migrating old JSON.
    """
    task = canonical.get('multi_daily_alt_acc', {})
    config = task.get('multi_daily_alt_acc_config', {})
    if not is_disabled_automatic_midnight(task):
        return []
    scheduler = task.get('scheduler', {})
    if not scheduler.get('enable', False):
        return []
    normal_config = (normal_round or {}).get('multi_daily_alt_acc_config', {})
    normal_scheduler = (normal_round or {}).get('scheduler', {})
    if (normal_scheduler.get('enable', False)
            and not normal_config.get(MANUAL_SCHEDULE_FIELD, False)
            and not normal_config.get('total_returngift_enable', False)
            and not normal_config.get('total_alliedteam_battle_enable', False)):
        # A newer writer already advanced the normal round. Retain its exact
        # timestamp/content, including a due morning; don't materialize it twice.
        target = datetime.fromisoformat(normal_scheduler['next_run'])
        values = {'total_' + key + '_enable': normal_config['total_' + key + '_enable']
                  for key in TASK_KEYS}
    else:
        plan = plan or load_task_plan()
        # The cancelled retry cannot postpone a legitimate early run.
        reference = now or datetime.now()
        morning = plan.schedule_target('morning', reference)
        if morning.target >= reference:
            phase, target = 'morning', morning.target
        else:
            afternoon = plan.schedule_target('afternoon', reference)
            if afternoon.target >= reference:
                phase, target = 'afternoon', afternoon.target
            else:
                phase = 'morning'
                target = plan.schedule_target(phase, reference + timedelta(days=1)).target
        values = {}
        for key in TASK_KEYS:
            value = plan.enabled(phase, key)
            if key in ('weekaward', 'consignment_ticket', 'mysteryshop'):
                value = value and target.weekday() in getattr(plan, key + '_weekdays')
            values['total_' + key + '_enable'] = value

    prefix = ('multi_daily_alt_acc', 'multi_daily_alt_acc_config')
    changed = []
    for field, value in values.items():
        if config.get(field) != value:
            config[field] = value
            changed.append(prefix + (field,))
    for field in ('total_returngift_enable', 'total_alliedteam_battle_enable'):
        if config.get(field) is not False:
            config[field] = False
            changed.append(prefix + (field,))
    scheduler['next_run'] = target.strftime('%Y-%m-%d %H:%M:%S')
    changed.append(('multi_daily_alt_acc', 'scheduler', 'next_run'))
    return changed
