"""Render the real finance template with synthetic data, without settings or APIs."""

import sys
from pathlib import Path

from jinja2 import StrictUndefined

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.webserver.templating import NAV_ITEMS, env


def render_demo() -> str:
    # Use the real template environment/filters, but never render() (which loads settings).
    demo_env = env.overlay(undefined=StrictUndefined)
    html = demo_env.get_template("finance.html").render(
        title="Финансы · ДЕМО",
        pretitle="Синтетические данные · пример интерфейса",
        active="finance",
        nav_items=NAV_ITEMS,
        csrf_token="",
        error="",
        msg="ДЕМО: все числа вымышлены и не являются результатами проекта. Действия отключены.",
        days=7,
        summary=dict(
            revenue_today=990,
            revenue_week=6990,
            revenue_month=6990,
            revenue_total=6990,
            paying_users=12,
            paid_count=18,
            arpu=139.8,
            avg_check=388.33,
        ),
        by_method=[dict(method="YooKassa", amount=6990, cnt=18)],
        total_methods=6990,
        spenders=[
            dict(
                user_id=i,
                username=f"demo_user_{i:02d}",
                full_name="",
                telegram_id=9000000000 + i,
                total=amount,
                cnt=2 if i <= 6 else 1,
                credits=0,
                last_paid=None,
            )
            for i, amount in enumerate(
                [1790, 690, 690, 690, 590, 490, 490, 390, 300, 290, 290, 290], 1
            )
        ],
        status_breakdown=dict(succeeded=18, pending=4, failed=2, canceled=1),
        funnel=dict(
            total_users=50,
            activated_users=30,
            activation_rate=60,
            paying_users=12,
            paid_rate=24,
            paid_from_active=40,
        ),
        pay_funnel=dict(
            total=25,
            succeeded=18,
            conv_rate=72,
            pending=4,
            failed=2,
            canceled=1,
            abandonment_rate=28,
            recovery_sent=10,
            recovery_recovered=2,
            recovery_rate=20,
        ),
        daily_labels=["01.10", "02.10", "03.10", "04.10", "05.10", "06.10", "07.10"],
        daily_amounts=[290, 690, 990, 1790, 1490, 750, 990],
    )
    # Static export: no form submissions, account actions or links to real admin routes.
    script = """<script>
    document.addEventListener('click', event => {
      if (event.target.closest('a, button')) event.preventDefault();
    });
    document.addEventListener('submit', event => event.preventDefault());
    document.querySelectorAll('input,button').forEach(el => el.disabled = true);
    </script>"""
    return html.replace("</body>", script + "</body>")


if __name__ == "__main__":
    output = ROOT / "docs/demo/finance.html"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_demo(), encoding="utf-8")
    print(output)
