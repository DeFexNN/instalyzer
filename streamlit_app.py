"""Instalyzer: a Streamlit frontend for the existing export + analysis pipeline."""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st

from web_backend import (
    ROOT, JobManager, format_duration, format_wait_range, live_configuration, normalize_profile,
    password_signature, read_config, result_cost,
)

st.set_page_config(page_title="Instalyzer — зрозумій профіль", page_icon="◈", layout="wide")
st.html("<style>" + (ROOT / "assets" / "style.css").read_text(encoding="utf-8") + "</style>")


@st.cache_resource
def manager() -> JobManager:
    return JobManager()


def settings() -> dict[str, str]:
    try:
        secrets = st.secrets.to_dict()
    except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
        secrets = {}
    return read_config(secrets)


def navigate(page: str, result_id: str | None = None) -> None:
    st.session_state.page = page
    if result_id:
        st.session_state.result_id = result_id


def authorized(config: dict) -> bool:
    return st.session_state.get("authenticated_for") == password_signature(config) and live_configuration(config)[0]


def demo_data() -> dict:
    return json.loads((ROOT / "demo" / "profile_analysis.json").read_text(encoding="utf-8"))


def show_sidebar(config: dict) -> None:
    with st.sidebar:
        st.html('<div class="brand"><span class="brand-mark">ıı</span>instalyzer</div><div class="brand-sub">Знайди сенс у стрічці</div>')
        for page, label, icon in (("new", "Новий аналіз", ":material/add_circle:"),
                                  ("history", "Мої аналізи", ":material/history:"),
                                  ("about", "Як це працює", ":material/help_outline:")):
            st.button(label, icon=icon, key=f"nav_{page}", width="stretch",
                type="primary" if st.session_state.page == page else "tertiary",
                on_click=navigate, args=(page,))
        st.space("medium")
        st.html('<div class="section-label">СПРОБУЙ БЕЗ НАЛАШТУВАНЬ</div>')
        st.button("Відкрити приклад", icon=":material/visibility:", width="stretch", key="sidebar_demo",
            on_click=navigate, args=("result", "demo"))
        st.caption("Вигаданий профіль. Готовий звіт, щоб познайомитися з форматом.")
        st.space("large")
        with st.container(border=True):
            ready, _ = live_configuration(config)
            st.markdown("**100 дописів. Повніша картина.**")
            st.caption("Фото, підписи та коментарі: професійні теми, хобі й особистий контекст.")
            st.html(f'<span class="pill{ "" if ready else " demo" }">● {"Live-доступ налаштовано" if ready else "Демонстраційний режим"}</span>')


def show_access(config: dict) -> None:
    ready, _ = live_configuration(config)
    if not ready:
        st.caption("Запуски ще не налаштовані власником. Готовий приклад доступний праворуч.")
        return
    if authorized(config):
        st.caption("✓ Доступ до запусків відкрито для цієї сесії.")
        return
    with st.popover("Ввести код доступу", icon=":material/lock:", width="stretch"):
        st.caption("Отримай код у власника демо, щоб запускати нові аналізи.")
        with st.form("access_form", clear_on_submit=True):
            password = st.text_input("Код доступу", type="password")
            submitted = st.form_submit_button("Відкрити доступ", width="stretch", type="primary")
        if submitted:
            try:
                if manager().unlock(st.session_state.owner_id, password, config):
                    st.session_state.authenticated_for = password_signature(config)
                    st.rerun()
                st.error("Код не підходить. Перевір його та спробуй ще раз.")
            except ValueError as exc:
                st.error(str(exc))


def show_home(config: dict) -> None:
    left, right = st.columns([1.55, 1], gap="large")
    with left:
        st.html('<div class="eyebrow">INSTAGRAM → INSIGHTS</div><div class="hero-title">За фотографіями —<br><span>ціла історія.</span></div><p class="hero-copy">Дізнайся, які інтереси й заняття простежуються у профілі. Фото та підписи перетворюються на один змістовний висновок.</p><div class="chips"><span class="chip">До 100 дописів</span><span class="chip">Фото + підписи + коментарі</span><span class="chip">Висновки з підставами</span></div>')
    with right:
        st.html('<div class="hero-art"><div class="art-kicker">ВІД ДЕТАЛЕЙ ДО ЗАГАЛЬНОЇ КАРТИНИ</div><div class="art-profile"><div class="art-avatar">a</div><div><div class="art-name">@dental_studio_demo</div><div class="art-small">Демонстраційний профіль</div></div></div><div class="art-topic"><span class="art-icon">↗</span><div><b>Стоматологічна практика</b><small>Повторювана тема у стрічці</small></div><span class="art-check">✓</span></div><div class="art-topic"><span class="art-icon" style="background:#eae7f5;color:#8a77b0">◉</span><div><b>Життя поза роботою</b><small>Згадки у підписах і відповідях автора</small></div><span class="art-check">✓</span></div></div>')

    main, example = st.columns([1.55, 1], gap="large")
    with main:
        with st.container(border=True, key="analysis_card"):
            st.subheader("Кого аналізуємо?")
            st.caption("Встав посилання на профіль, доступний Instagram-акаунту власника демо.")
            show_access(config)
            with st.form("analysis_form", border=False):
                profile = st.text_input("Посилання на Instagram", placeholder="https://www.instagram.com/username/", key="profile_input")
                consent = st.checkbox("Дозволяю передати фото, підписи й коментарі для аналізу. Contributor-модель може використовувати їх для навчання.", key="training_consent")
                run = st.form_submit_button("Проаналізувати профіль  →", type="primary", width="stretch",
                    disabled=not authorized(config) or bool(st.session_state.get("active_job")))
            if run:
                if not consent:
                    st.error("Для запуску потрібно підтвердити передачу матеріалів моделі.")
                else:
                    try:
                        username = normalize_profile(profile)
                        job_id = manager().submit(st.session_state.owner_id, username, config, authorized=authorized(config))
                        st.session_state.active_job = job_id
                        st.rerun()
                    except ValueError as exc:
                        st.error(str(exc))
            st.caption("Новий аналіз може зайняти кілька хвилин. Час залежить від кількості й типів медіа.")
    with example:
        with st.container(border=True, key="example_card"):
            st.html('<span class="pill demo">ПРИКЛАД ЗВІТУ</span><div class="preview-title">Спочатку поглянь,<br>що ти отримаєш.</div><div class="preview-text">Загальний портрет стрічки, ймовірні інтереси та чесне пояснення меж висновків.</div><div class="topic-row"><span class="topic">Професійна тематика</span><span class="topic">Хобі</span><span class="topic">Сімейний контекст</span></div>')
            st.button("Переглянути демо-звіт", icon=":material/arrow_forward:", width="stretch", key="home_demo",
                on_click=navigate, args=("result", "demo"))
    st.html('<div class="section-label">ТРИ ПРОСТІ КРОКИ</div><div class="step-grid"><div><span class="step-num">01</span><b>Додай профіль</b><p>Посилання або нік. Збираємо до 100 доступних дописів.</p></div><div><span class="step-num">02</span><b>Знаходимо теми</b><p>Порівнюємо фото й підписи, відділяючи факти від припущень.</p></div><div><span class="step-num">03</span><b>Отримай висновок</b><p>Прочитай звіт і завантаж його одним натисканням.</p></div></div>')


def show_running(config: dict) -> None:
    job_id = st.session_state.get("active_job")
    if not job_id:
        return

    @st.fragment(run_every="1s")
    def progress_panel():
        job = manager().snapshot(st.session_state.owner_id, job_id)
        if job["state"] == "complete":
            st.session_state.active_job = None
            navigate("result", job_id)
            st.rerun()
        with st.container(border=True):
            title, clock = st.columns([4, 1])
            title.markdown(f"**Аналіз @{job['username']}**")
            clock.caption(format_duration(job["queue_elapsed"] if job["state"] == "queued" else job["elapsed"]))
            if job["state"] in {"failed", "cancelled"}:
                st.error(job["error"]) if job["state"] == "failed" else st.info(job["error"])
                if job["can_resume"] and authorized(config) and st.button("Продовжити збережений аналіз", key="resume_job"):
                    try:
                        manager().resume(st.session_state.owner_id, job_id, config, authorized=True)
                        st.rerun()
                    except ValueError as exc:
                        st.error(str(exc))
                if st.button("Повернутися до нового запуску", key="reset_job"):
                    st.session_state.active_job = None
                    navigate("new")
                    st.rerun()
                return
            stages = ("scraping", "comments", "media", "analysis", "synthesis")
            labels = ("Збір дописів", "Коментарі", "Завантаження медіа", "Аналіз матеріалів", "Фінальний висновок")
            current = stages.index(job["stage"]) if job["stage"] in stages else -1
            parts = [f'<span class="run-step {"done" if i < current else "active" if i == current else ""}">{"✓ " if i < current else ""}{label}</span>' for i, label in enumerate(labels)]
            st.html('<div class="run-steps">' + "".join(parts) + '</div>')
            messages = {"scraping": "Читаємо доступні дописи профілю…", "comments": f"Перевірено коментарі у {job['done']} із {job['total']} дописів.", "media": f"Підготовлено {job['done']} із {job['total']} дописів.", "analysis": f"Проаналізовано {job['done']} із {job['total']} дописів.", "synthesis": "Усі матеріали оброблено. Формуємо спільний висновок…"}
            if job["state"] == "queued":
                position, wait = st.columns(2)
                position.metric("Ваше місце в черзі", f"{job['queue_position']} з {job['queue_total']}",
                    help="Враховано поточний запуск, який уже виконується, і запуски, що очікують.")
                wait.metric("Приблизно до старту", format_wait_range(job["wait_seconds_min"], job["wait_seconds_max"]),
                    help="Очікування до початку вашого аналізу. Час самого аналізу сюди не входить.")
                if job["jobs_ahead"]:
                    st.caption(f"Запусків попереду: {job['jobs_ahead']}. Зараз виконується: {job['running_jobs']}. Запуски виконуються по одному.")
                    basis = (f"Оцінка за {job['wait_estimate_samples']} завершеними запусками на цьому сервері."
                             if job["wait_estimate_basis"] == "history" else "Початкова груба оцінка: завершених запусків для порівняння ще немає.")
                    st.caption(basis + " Позиція й час оновлюються щосекунди; тривалість залежить від медіа, коментарів і відповіді сервісів.")
                    if job["wait_estimate_overdue"]:
                        st.caption("Поточний запуск триває довше за орієнтир. Оцінка очікування може збільшитися.")
                else:
                    st.caption("Ви перші в черзі. Готуємо запуск…")
            else:
                st.caption(messages.get(job["stage"], "Працюємо…"))
            if job["stage"] in {"comments", "media", "analysis"}:
                st.progress(min(1.0, job["done"] / max(1, job["total"])))
            if st.button("Зупинити", type="tertiary", icon=":material/stop_circle:", key="cancel_job"):
                manager().cancel(st.session_state.owner_id, job_id)
                st.info("Зупиняємо поточні запити…")
    progress_panel()


def show_result() -> None:
    result_id = st.session_state.get("result_id", "demo")
    is_demo = result_id == "demo"
    try:
        payload = demo_data() if is_demo else manager().result(st.session_state.owner_id, result_id)
    except (ValueError, OSError):
        st.info("Цей звіт більше недоступний. Відкрий приклад або запусти новий аналіз.")
        return
    username = str(payload.get("account") or "profile").lstrip("@")
    if st.button("Новий аналіз", icon=":material/arrow_back:", type="tertiary", key="result_back"):
        navigate("new")
        st.rerun()
    st.html('<span class="pill demo">ДЕМОНСТРАЦІЙНИЙ ПРИКЛАД</span>' if is_demo else '<span class="pill">✓ АНАЛІЗ ЗАВЕРШЕНО</span>')
    st.title(f"Профіль @{username}")
    st.caption("Вигаданий приклад професійного профілю з особистими епізодами. Цифри демонстраційні." if is_demo else "Професійні теми, можливі хобі, прямо описані сімейні зв’язки та контекст коментарів.")
    a, comments_metric, b, c = st.columns(4)
    a.metric("Дописів у вибірці", str(payload.get("post_count", 0)))
    coverage = payload.get("comment_coverage") or {}
    if not is_demo and coverage.get("posts_not_collected"):
        st.warning("Цей збережений звіт створено без збору коментарів. Щоб врахувати їх, запусти новий аналіз профілю.")
    comments_metric.metric("Коментарів проаналізовано", str(coverage["analyzed"]) if "analyzed" in coverage else "—",
        help="Кількість коментарів, переданих моделі. Повнота залежить від доступності, обмеження кількості та обсягу тексту.")
    execution = payload.get("web_execution") or {}
    b.metric("Повний час запуску", format_duration(execution.get("total_duration_seconds", payload.get("analysis_duration_seconds"))),
        help="Для нового вебзапуску: збір постів, завантаження, підготовка медіа та всі запити ШІ. При продовженні показує час поточного запуску.")
    cost = result_cost(payload)
    c.metric("Оцінка вартості API", f"${cost:.5f}" if cost is not None else "—", help="Оцінка за токенами та збереженим тарифом. Не включає хостинг. У прикладі реальних запитів немає.")
    st.space("small")
    summary, materials, details = st.tabs(["Висновки", "Матеріали", "Про аналіз"])
    with summary:
        st.html('<div class="report-intro">Професійна тематика й особисті епізоди розглядаються окремо. Для припущень зазначено джерело, підстави та впевненість.</div>')
        st.markdown(str(payload.get("profile_analysis") or "Висновок відсутній."))
    with materials:
        if is_demo:
            st.caption("Схематичні ілюстрації тем вигаданого прикладу. Реальних фото тут немає.")
            st.html('<div class="demo-visuals"><div class="demo-visual"><span class="symbol">⌁</span><strong>Стоматологічна практика</strong></div><div class="demo-visual"><span class="symbol">◉</span><strong>Музичне захоплення</strong></div><div class="demo-visual"><span class="symbol">△</span><strong>Прямо описаний сімейний епізод</strong></div></div>')
        else:
            previews = manager().media_preview(st.session_state.owner_id, result_id)
            st.caption("Попередній перегляд до 12 завантажених фото. Для каруселі за замовчуванням аналізуються до шести рівномірно вибраних елементів; для відео — один кадр.")
            if previews:
                columns = st.columns(3)
                for i, path in enumerate(previews):
                    columns[i % 3].image(str(path), width="stretch")
            else:
                st.info("Окремих фото для перегляду немає. У матеріалах можуть бути лише відео.")
    with details:
        st.markdown("**Що враховано**")
        st.write("Фото, вибрані кадри відео й підписи. Аналіз описує видимі заняття та повторювані теми. Він не встановлює приховані особисті обставини.")
        settings_data = payload.get("analysis_settings") or {}
        st.caption(f"Модель: {payload.get('model', '—')} · До {settings_data.get('max_media_per_post', 6)} медіа на допис · Фото до {settings_data.get('image_max_edge', 768)} px")
        if coverage:
            st.caption(f"Коментарі: зібрано {coverage.get('collected', 0)}, проаналізовано {coverage.get('analyzed', 0)}, поза текстовою вибіркою {coverage.get('sampled_out', 0)}. Охоплюються основні коментарі та доступні попередні відповіді, а не всі гілки.")
            if coverage.get("posts_unavailable"):
                st.warning(f"Коментарі недоступні для {coverage['posts_unavailable']} дописів. Причиною можуть бути налаштування Instagram або обмеження запитів.")
            if coverage.get("posts_limited"):
                st.caption(f"Дописів з обмеженою кількістю зібраних коментарів: {coverage['posts_limited']}.")
            if coverage.get("posts_not_collected"):
                st.info(f"Для {coverage['posts_not_collected']} дописів у цьому збереженому звіті коментарі не збиралися.")
            if coverage.get("texts_truncated"):
                st.caption(f"Частково скорочених текстів коментарів: {coverage['texts_truncated']}.")
        if not is_demo:
            skipped = sum(int(p.get("media_skipped", 0)) for p in payload.get("posts", []))
            if skipped:
                st.warning(f"Недоступних елементів у вибірці: {skipped}. Висновок спирається на решту матеріалів.")
        if execution:
            st.caption(f"Збір і завантаження: {format_duration(execution.get('scrape_duration_seconds'))} · Аналіз із підготовкою медіа: {format_duration(payload.get('analysis_duration_seconds'))}.")
        st.caption("Підготовка фото й кадрів уже входить у час аналізу. При продовженні після помилки час стосується поточного запуску; вартість охоплює збережені успішні запити до моделі.")
    st.divider()
    md = f"# Підсумковий аналіз профілю @{username}\n\n{payload.get('profile_analysis', '')}\n".encode("utf-8") if is_demo else manager().artifact(st.session_state.owner_id, result_id, "profile_analysis.md")
    raw_json = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") if is_demo else manager().artifact(st.session_state.owner_id, result_id, "profile_analysis.json")
    download, technical, _ = st.columns([1.2, 1.1, 1])
    download.download_button("Завантажити звіт .md", data=md, file_name=f"instalyzer-{username}.md", mime="text/markdown", icon=":material/download:", type="primary", width="stretch")
    technical.download_button("Дані аналізу .json", data=raw_json, file_name=f"instalyzer-{username}.json", mime="application/json", width="stretch")


def show_history() -> None:
    st.html('<div class="eyebrow">ТВОЇ РЕЗУЛЬТАТИ</div>')
    st.title("Мої аналізи")
    st.caption("Історія цієї браузерної сесії. Завантаж важливі звіти: перезапуск хостингу очищає тимчасові результати.")
    jobs = manager().history(st.session_state.owner_id)
    if not jobs:
        st.html('<div class="empty"><div class="symbol">◈</div><b>Перший висновок ще попереду.</b><p>Додай профіль або відкрий приклад готового звіту.</p></div>')
        st.button("Почати аналіз", type="primary", on_click=navigate, args=("new",))
        return
    status = {"queued": "У черзі", "running": "В обробці", "complete": "Готово", "failed": "Помилка", "cancelled": "Зупинено"}
    for job in jobs:
        with st.container(border=True):
            name, when, action = st.columns([2, 1.3, 1])
            name.markdown(f"**@{job['username']}**")
            name.caption(status.get(job["state"], job["state"]))
            when.caption(datetime.fromtimestamp(job["created"], ZoneInfo("Europe/Kyiv")).strftime("%d.%m.%Y · %H:%M"))
            when.write(format_duration(job["elapsed"]))
            if job["state"] == "complete":
                action.button("Відкрити звіт", key=f"open_{job['id']}", width="stretch", on_click=navigate, args=("result", job["id"]))
            elif action.button("До процесу", key=f"watch_{job['id']}", width="stretch"):
                st.session_state.active_job = job["id"]
                navigate("new")
                st.rerun()


def show_about(config: dict) -> None:
    st.html('<div class="eyebrow">БІЛЬШЕ КОНТЕКСТУ, МЕНШЕ ЗДОГАДОК</div>')
    st.title("Що бачить Instalyzer")
    st.write("Застосунок поєднує фото, підписи й доступні коментарі. Окремо розглядає професійний контент, можливі хобі, особисті епізоди й прямо названі автором сімейні зв’язки.")
    for title, body in (("Збираємо до 100 дописів", "Instagram відкривається через налаштований сеанс власника демо. Зберігаються доступні фото й відео, дати, підписи й до 100 коментарів на допис із доступними попередніми відповідями."),
        ("Порівнюємо матеріали", "Зображення готуються до аналізу. Для каруселі беруться до шести рівномірно вибраних елементів; для відео — один кадр. Така вибірка має обмеження."),
        ("Формуємо один звіт", "Окремі розділи про професійні теми, хобі, прямо описаний сімейний контекст, коментарі, інші деталі, межі висновків і підсумок. Окремі публікації у фінальному Markdown не перелічуються.")):
        with st.container(border=True):
            st.subheader(title)
            st.write(body)
    st.info("Використовується Contributor-модель. Надіслані фото, підписи, коментарі та відповіді можуть використовуватися Meta для навчання. Перед кожним новим запуском це потрібно підтвердити.")
    with st.expander("Налаштування для власника демо"):
        ready, missing = live_configuration(config)
        st.write("Live-запуски готові." if ready else "Для live-запусків додай конфігурацію у Streamlit Secrets.")
        if missing:
            st.code("\n".join(missing), language="text")
        st.caption("Приклад конфігурації: .streamlit/secrets.toml.example. Самі значення cookies та ключів інтерфейс не показує.")
        st.markdown("[Інструкція Streamlit Secrets](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/secrets-management)")


def main() -> None:
    st.session_state.setdefault("owner_id", uuid.uuid4().hex)
    st.session_state.setdefault("page", "new")
    config = settings()
    show_sidebar(config)
    show_running(config)
    page = st.session_state.page
    if page == "result":
        show_result()
    elif page == "history":
        show_history()
    elif page == "about":
        show_about(config)
    else:
        show_home(config)
    st.html('<div class="footer-note">Instalyzer · Висновки за доступними матеріалами. Припущення завжди потребують перевірки.</div>')


if __name__ == "__main__":
    main()
