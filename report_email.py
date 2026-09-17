#!/usr/bin/env python3
"""Build and send the maintenance fleet report email."""

import json
import os
import platform
import smtplib
import ssl
import subprocess
import urllib.request
from email.message import EmailMessage

BODY_BG = '#f4f5f7'
CARD_BG = '#ffffff'
BORDER = '#e5e7eb'
TEXT = '#1f2933'
MUTED = '#6b7280'
HEADER_BG = '#12372a'

TH = ('padding:10px 14px;background:#f9fafb;border-bottom:1px solid %s;'
      'color:#374151;font-size:11px;text-transform:uppercase;'
      'letter-spacing:.05em;text-align:left;white-space:nowrap;' % BORDER)
TD = 'padding:11px 14px;border-bottom:1px solid %s;color:%s;font-size:13px;' % (BORDER, TEXT)
TD_NUM = TD + 'text-align:right;white-space:nowrap;'


def env(name, default=''):
    value = os.environ.get(name)
    return value if value else default


def escape(value):
    return (str(value).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))


def read_os_release():
    try:
        with open('/etc/os-release') as handle:
            for line in handle:
                if line.startswith('PRETTY_NAME='):
                    return line.split('=', 1)[1].strip().strip('"')
    except OSError:
        pass
    return platform.system()


def read_memory():
    values = {}
    try:
        with open('/proc/meminfo') as handle:
            for line in handle:
                key, value = line.split(':', 1)
                values[key] = int(value.split()[0])
    except OSError:
        return None, None
    return round(values.get('MemTotal', 0) / 1048576, 1), round(values.get('MemAvailable', 0) / 1048576, 1)


def disk_free_percent():
    try:
        stats = os.statvfs('/')
        if stats.f_blocks:
            return round(100.0 * stats.f_bavail / stats.f_blocks, 1)
    except OSError:
        pass
    return None


def failed_unit_count():
    if not os.path.isdir('/run/systemd/system'):
        return None
    try:
        result = subprocess.run(
            ['systemctl', '--failed', '--no-legend'],
            capture_output=True, text=True, timeout=20,
        )
        return len([line for line in result.stdout.splitlines() if line.strip()])
    except Exception:
        return None


def api_get(base_url, token, path):
    request = urllib.request.Request(base_url.rstrip('/') + path)
    request.add_header('Authorization', 'Bearer ' + token)
    context = ssl.create_default_context()
    with urllib.request.urlopen(request, context=context, timeout=30) as response:
        return json.loads(response.read().decode())


def workflow_steps(base_url, token):
    if not base_url or not token:
        return [], None
    try:
        jobs = api_get(base_url, token, '/api/controller/v2/workflow_jobs/?order_by=-id&page_size=10')
        results = jobs.get('results', [])
        current = None
        for item in results:
            if item.get('status') in ('running', 'pending'):
                current = item
                break
        if current is None and results:
            current = results[0]
        if current is None:
            return [], None
        nodes = api_get(
            base_url, token,
            '/api/controller/v2/workflow_jobs/%d/workflow_nodes/' % current['id'],
        )
        steps = []
        for node in nodes.get('results', []):
            summary = node.get('summary_fields', {})
            job = summary.get('job') or {}
            steps.append({
                'name': summary.get('unified_job_template', {}).get('name', 'Unknown'),
                'status': job.get('status', 'pending'),
                'job': job.get('id'),
            })
        return steps, current.get('status')
    except Exception:
        return [], None


def badge(status):
    text = (status or 'unknown').lower()
    if text in ('successful', 'ok', 'completed', 'created'):
        background, color = '#e7f5ee', '#0b6b3a'
    elif text in ('failed', 'error', 'problem', 'canceled'):
        background, color = '#fdecec', '#b42318'
    else:
        background, color = '#fff4e5', '#92400e'
    return ('<span style="display:inline-block;padding:3px 12px;border-radius:999px;'
            'font-size:12px;font-weight:600;background:%s;color:%s;">%s</span>'
            % (background, color, escape(status or 'unknown')))


def section_title(text):
    return ('<div style="font-size:11px;font-weight:700;text-transform:uppercase;'
            'letter-spacing:.06em;color:%s;margin:0 0 10px 0;">%s</div>' % (MUTED, escape(text)))


def summary_banner(workflow_status, steps, fleet_rows):
    problems = [row for row in fleet_rows if row.get('status') != 'OK']
    if workflow_status == 'successful' and not problems:
        background, color, text = '#e7f5ee', '#0b6b3a', 'All workflow steps and all machines are healthy'
    elif workflow_status in ('failed', 'error', 'canceled') or problems:
        failed = [step['name'] for step in steps if step['status'] in ('failed', 'error', 'canceled')]
        background, color = '#fdecec', '#b42318'
        parts = []
        if failed:
            parts.append('failed steps: ' + ', '.join(failed))
        if problems:
            parts.append('machines with problems: ' + ', '.join(row['machine'] for row in problems))
        text = 'Attention needed - ' + '; '.join(parts)
    elif workflow_status in ('running', 'pending'):
        background, color, text = '#fff4e5', '#92400e', 'Workflow is still running'
    else:
        background, color, text = '#eef2f6', '#475467', 'Workflow summary is not available'
    return ('<div style="background:%s;color:%s;padding:12px 16px;border-radius:8px;'
            'font-size:14px;font-weight:600;margin-bottom:22px;">%s</div>'
            % (background, color, escape(text)))


def steps_table(steps):
    if not steps:
        return ('<div style="font-size:13px;color:%s;padding:12px 16px;background:#f9fafb;'
                'border-radius:8px;">Step results are available in the AAP job output.</div>' % MUTED)
    rows = []
    for index, step in enumerate(steps):
        background = CARD_BG if index % 2 == 0 else '#fbfcfd'
        job = '#%s' % step['job'] if step.get('job') else ''
        rows.append(
            '<tr style="background:%s;">'
            '<td style="%s">%s</td><td style="%s">%s</td>'
            '<td style="%s" align="right">%s</td></tr>'
            % (background, TD, escape(step['name']), TD, badge(step['status']), TD, escape(job))
        )
    return (
        '<table role="presentation" width="100%%" cellpadding="0" cellspacing="0" '
        'style="border-collapse:collapse;border:1px solid %s;border-radius:8px;overflow:hidden;">'
        '<tr><th style="%s">Step</th><th style="%s">Result</th>'
        '<th style="%s" align="right">Job</th></tr>%s</table>'
        % (BORDER, TH, TH, TH, ''.join(rows))
    )


def fleet_table(fleet_rows):
    if not fleet_rows:
        return ('<div style="font-size:13px;color:%s;padding:12px 16px;background:#f9fafb;'
                'border-radius:8px;">No machine results were reported.</div>' % MUTED)
    rows = []
    for index, row in enumerate(fleet_rows):
        background = CARD_BG if index % 2 == 0 else '#fbfcfd'
        rows.append(
            '<tr style="background:%s;">'
            '<td style="%s"><strong>%s</strong></td>'
            '<td style="%s">%s</td>'
            '<td style="%s">%s%%</td>'
            '<td style="%s">%s</td>'
            '<td style="%s" align="right">%s</td>'
            '</tr>' % (background, TD, escape(row.get('machine', 'unknown')), TD,
                       escape(row.get('kernel', 'unknown')), TD_NUM,
                       escape(row.get('disk_free', 'n/a')), TD_NUM,
                       escape(row.get('failed_units', 'n/a')), TD,
                       badge(row.get('status', 'unknown')))
        )
    return (
        '<table role="presentation" width="100%%" cellpadding="0" cellspacing="0" '
        'style="border-collapse:collapse;border:1px solid %s;border-radius:8px;overflow:hidden;">'
        '<tr><th style="%s">Machine</th><th style="%s">Kernel</th>'
        '<th style="%s" align="right">Disk free</th>'
        '<th style="%s" align="right">Failed units</th>'
        '<th style="%s" align="right">Result</th></tr>%s</table>'
        % (BORDER, TH, TH, TH, TH, TH, ''.join(rows))
    )


def build_html(timestamp, steps, workflow_status, fleet_rows, message, signature):
    total = len(fleet_rows)
    problems = len([row for row in fleet_rows if row.get('status') != 'OK'])
    summary_line = ''
    if total:
        summary_line = ('<div style="font-size:13px;color:%s;margin:12px 0 0 0;">'
                        '%d machines checked &middot; %d healthy &middot; %d with problems</div>'
                        % (MUTED, total, total - problems, problems))
    message_block = ''
    if message and message.strip():
        message_block = (
            '<div style="margin-top:24px;">%s<div style="background:#f9fafb;border-left:3px solid %s;'
            'padding:14px 16px;border-radius:0 8px 8px 0;font-size:14px;color:%s;'
            'white-space:pre-wrap;">%s</div></div>'
            % (section_title('Your message'), HEADER_BG, TEXT, escape(message.strip()))
        )
    signature_block = ''.join('<div>%s</div>' % escape(line) for line in signature.splitlines())
    return (
        '<!doctype html><html><body style="margin:0;padding:0;background:%s;">'
        '<table role="presentation" width="100%%" cellpadding="0" cellspacing="0" '
        'style="background:%s;padding:28px 12px;"><tr><td align="center">'
        '<table role="presentation" width="700" cellpadding="0" cellspacing="0" '
        'style="width:700px;max-width:700px;background:%s;border-radius:12px;overflow:hidden;'
        'border:1px solid %s;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">'
        '<tr><td style="background:%s;padding:26px 28px;">'
        '<div style="color:#ffffff;font-size:20px;font-weight:700;">Fleet Maintenance Report</div>'
        '<div style="color:#a7d3c1;font-size:13px;margin-top:6px;">%d machines &nbsp;&middot;&nbsp; %s</div>'
        '</td></tr>'
        '<tr><td style="padding:26px 28px 6px;">%s</td></tr>'
        '<tr><td style="padding:6px 28px 0;">%s%s</td></tr>'
        '<tr><td style="padding:24px 28px 6px;">%s%s</td></tr>'
        '<tr><td style="padding:6px 28px 30px;">%s</td></tr>'
        '<tr><td style="background:#f9fafb;border-top:1px solid %s;padding:20px 28px;'
        'text-align:center;color:%s;font-size:12px;line-height:1.7;">%s</td></tr>'
        '</table></td></tr></table></body></html>'
        % (BODY_BG, BODY_BG, CARD_BG, BORDER, HEADER_BG, total, escape(timestamp),
           summary_banner(workflow_status, steps, fleet_rows),
           section_title('Workflow steps'), steps_table(steps),
           section_title('Machine results') + summary_line, fleet_table(fleet_rows),
           message_block, BORDER, MUTED, signature_block)
    )


def build_text(timestamp, steps, workflow_status, fleet_rows, message, signature):
    lines = ['Fleet maintenance report', 'Generated %s' % timestamp, '']
    if workflow_status:
        lines += ['Workflow status: %s' % workflow_status, '']
    if steps:
        lines.append('Workflow steps')
        for step in steps:
            lines.append('  %-16s %-12s %s' % (step['name'], step['status'],
                                               '#%s' % step['job'] if step.get('job') else ''))
        lines.append('')
    if fleet_rows:
        lines.append('Machine results')
        lines.append('  %-10s %-26s %-10s %-8s %s' % ('Machine', 'Kernel', 'Disk', 'Failed', 'Result'))
        for row in fleet_rows:
            lines.append('  %-10s %-26s %-10s %-8s %s' % (
                row.get('machine', ''), row.get('kernel', ''), '%s%%' % row.get('disk_free', ''),
                row.get('failed_units', ''), row.get('status', '')))
    if message and message.strip():
        lines += ['', message.strip()]
    lines += ['', signature]
    return '\n'.join(lines)


def main():
    username = env('SMTP_USER')
    password = env('SMTP_PASS')
    outcome_file = env('OUTCOME_FILE', '/tmp/maintenance_email_result.txt')
    html_file = env('REPORT_HTML_FILE', '/tmp/maintenance_report.html')
    signature = env('SMTP_SIGNATURE')

    fleet_rows = []
    raw_rows = env('HOST_DATA_JSON')
    if raw_rows:
        try:
            fleet_rows = json.loads(raw_rows)
        except ValueError:
            fleet_rows = []

    if not fleet_rows:
        free_disk = disk_free_percent()
        fleet_rows = [{
            'machine': platform.node() or 'host',
            'kernel': platform.release(),
            'disk_free': free_disk if free_disk is not None else 'n/a',
            'failed_units': failed_unit_count() if failed_unit_count() is not None else 'n/a',
            'status': 'OK' if (free_disk is None or free_disk >= 10) else 'PROBLEM',
        }]

    steps, workflow_status = workflow_steps(env('AAP_CONTROLLER_URL'), env('AAP_CONTROLLER_TOKEN'))
    timestamp = subprocess.run(['date', '-u', '+%Y-%m-%d %H:%M UTC'],
                               capture_output=True, text=True).stdout.strip()

    html = build_html(timestamp, steps, workflow_status, fleet_rows, env('SMTP_MESSAGE'), signature)
    text = build_text(timestamp, steps, workflow_status, fleet_rows, env('SMTP_MESSAGE'), signature)

    try:
        with open(html_file, 'w') as handle:
            handle.write(html)
    except OSError:
        pass

    try:
        message = EmailMessage()
        message['From'] = username
        message['To'] = env('SMTP_TO')
        message['Subject'] = env('SMTP_SUBJECT', 'Maintenance result')
        message.set_content(text)
        message.add_alternative(html, subtype='html')

        context = ssl.create_default_context()
        with smtplib.SMTP(env('SMTP_HOST', 'smtp.gmail.com'),
                          int(env('SMTP_PORT', '587')), timeout=30) as server:
            if env('SMTP_STARTTLS', 'true').lower() == 'true':
                server.starttls(context=context)
            server.login(username, password)
            server.send_message(message)
        outcome = 'Email sent to %s (%d machines)' % (message['To'], len(fleet_rows))
    except Exception as error:
        detail = str(error).replace(password, '***').replace(username, '***')
        outcome = 'SMTP failure: %s: %s' % (type(error).__name__, detail)

    with open(outcome_file, 'w') as handle:
        handle.write(outcome + '\n')
    print(outcome)


if __name__ == '__main__':
    main()
