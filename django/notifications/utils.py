from django.utils import timezone as tz


def is_rule_active_now(rule):
    now = tz.localtime()
    if rule.valid_from and now < rule.valid_from:
        return False
    if rule.valid_until and now > rule.valid_until:
        return False
    if rule.schedule:
        today = now.strftime("%a").lower()[:3]
        blocks = rule.schedule.get(today, [])
        if not blocks:
            return False
        t = now.strftime("%H:%M")
        if not any(start <= t < end for start, end in blocks):
            return False
    return rule.is_active
