from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path

from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    ContentSwitcher,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    ProgressBar,
    RichLog,
    Select,
    SelectionList,
    Static,
    TextArea,
)

from .jobs import JobRunner
from .models import Account, CnameTarget, JobProgress, JobResult, Zone
from .services import (
    CloudflareClient,
    find_zones_across_accounts,
    list_all_zones,
    load_cnames_for_zones,
)
from .storage import Storage, unique_domains


def display_cname(zone: Zone) -> str:
    if zone.current_cname:
        return zone.current_cname
    return "нет CNAME" if zone.cname_loaded else "не загружен"


class ConfirmModal(ModalScreen[bool]):
    def __init__(self, title: str, message: str, *, danger: bool = False):
        super().__init__()
        self.dialog_title = title
        self.message = message
        self.danger = danger

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.dialog_title, classes="dialog-title")
            yield Static(self.message, classes="dialog-message")
            with Horizontal(classes="dialog-actions"):
                yield Button("Отмена", id="cancel")
                yield Button(
                    "Подтвердить",
                    id="confirm",
                    variant="error" if self.danger else "primary",
                )

    @on(Button.Pressed)
    def handle_button(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")


class NoticeModal(ModalScreen[None]):
    def __init__(self, title: str, message: str):
        super().__init__()
        self.dialog_title = title
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.dialog_title, classes="dialog-title")
            yield Static(self.message, classes="dialog-message")
            yield Button("Закрыть", id="close", variant="primary")

    @on(Button.Pressed, "#close")
    def close(self) -> None:
        self.dismiss(None)


class AccountModal(ModalScreen[Account | None]):
    def __init__(self, account: Account | None = None):
        super().__init__()
        self.account = account

    def compose(self) -> ComposeResult:
        account = self.account
        with Vertical(classes="dialog form-dialog"):
            yield Label(
                "Редактирование аккаунта" if account else "Новый аккаунт",
                classes="dialog-title",
            )
            yield Label("Название")
            yield Input(account.label if account else "", id="label")
            yield Label("Email")
            yield Input(account.email if account else "", id="email")
            yield Label("Global API Key")
            yield Input(
                account.api_key if account else "", id="api-key", password=True
            )
            yield Static("", id="form-error", classes="error-text")
            with Horizontal(classes="dialog-actions"):
                yield Button("Отмена", id="cancel")
                yield Button("Сохранить", id="save", variant="primary")

    @on(Button.Pressed)
    def handle_button(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        try:
            account = Account.from_dict(
                {
                    "label": self.query_one("#label", Input).value,
                    "email": self.query_one("#email", Input).value,
                    "api_key": self.query_one("#api-key", Input).value,
                }
            )
        except ValueError as error:
            self.query_one("#form-error", Static).update(str(error))
            return
        self.dismiss(account)


class CnameModal(ModalScreen[CnameTarget | None]):
    def __init__(self, cname: CnameTarget | None = None):
        super().__init__()
        self.cname = cname

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog form-dialog"):
            yield Label(
                "Редактирование CNAME" if self.cname else "Новый CNAME",
                classes="dialog-title",
            )
            yield Label("Название")
            yield Input(self.cname.name if self.cname else "", id="name")
            yield Label("Target")
            yield Input(self.cname.target if self.cname else "", id="target")
            yield Static("", id="form-error", classes="error-text")
            with Horizontal(classes="dialog-actions"):
                yield Button("Отмена", id="cancel")
                yield Button("Сохранить", id="save", variant="primary")

    @on(Button.Pressed)
    def handle_button(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        try:
            cname = CnameTarget.from_dict(
                {
                    "name": self.query_one("#name", Input).value,
                    "target": self.query_one("#target", Input).value,
                }
            )
        except ValueError as error:
            self.query_one("#form-error", Static).update(str(error))
            return
        self.dismiss(cname)


JobFactory = Callable[
    [list[str] | None, JobRunner, Callable[[JobProgress, JobResult], None]],
    Awaitable[JobProgress],
]


class JobModal(ModalScreen[JobProgress | None]):
    def __init__(
        self,
        storage: Storage,
        title: str,
        total: int,
        factory: JobFactory,
        *,
        invalidate_cache: bool = True,
    ):
        super().__init__()
        self.storage = storage
        self.job_title = title
        self.total = total
        self.factory = factory
        self.invalidate_cache = invalidate_cache
        self.runner = JobRunner(storage, log=self.write_log)
        self.result: JobProgress | None = None
        self.running = False

    def compose(self) -> ComposeResult:
        with Vertical(classes="job-dialog"):
            yield Label(self.job_title, classes="dialog-title")
            yield Static(f"Ожидается операций: {self.total}", id="job-status")
            yield ProgressBar(total=max(1, self.total), show_eta=True, id="job-progress")
            with Horizontal(classes="job-body"):
                yield RichLog(id="job-log", wrap=True, markup=True)
                yield DataTable(id="job-results", zebra_stripes=True)
            with Horizontal(classes="dialog-actions"):
                yield Button("Отменить", id="cancel-job", variant="warning")
                yield Button("Повторить ошибки", id="retry", disabled=True)
                yield Button("Закрыть", id="close-job", disabled=True, variant="primary")

    def on_mount(self) -> None:
        table = self.query_one("#job-results", DataTable)
        table.add_columns("Домен", "Аккаунт", "Результат", "Сообщение")
        self.start_job(None)

    def write_log(self, message: str) -> None:
        if self.is_mounted:
            self.query_one("#job-log", RichLog).write(message)

    def update_progress(self, progress: JobProgress, result: JobResult) -> None:
        bar = self.query_one("#job-progress", ProgressBar)
        bar.update(total=max(1, progress.total), progress=progress.done)
        self.query_one("#job-status", Static).update(
            f"Готово {progress.done}/{progress.total}  "
            f"Успешно: {progress.succeeded}  Ошибок: {progress.failed}"
        )
        self.query_one("#job-results", DataTable).add_row(
            result.domain,
            result.account,
            "OK" if result.success else "Ошибка",
            result.message,
        )

    def start_job(self, failed_domains: list[str] | None) -> None:
        self.running = True
        self.query_one("#cancel-job", Button).disabled = False
        self.query_one("#close-job", Button).disabled = True
        self.query_one("#retry", Button).disabled = True
        if failed_domains is not None:
            self.query_one("#job-results", DataTable).clear()
            self.query_one("#job-log", RichLog).clear()
            self.write_log(f"Повтор: {len(failed_domains)} доменов")
        self.run_worker(self._execute(failed_domains), exclusive=True, group="job")

    async def _execute(self, failed_domains: list[str] | None) -> None:
        try:
            self.result = await self.factory(
                failed_domains, self.runner, self.update_progress
            )
            status = "Отменено" if self.result.cancelled else "Завершено"
            self.query_one("#job-status", Static).update(
                f"{status}. Успешно: {self.result.succeeded}; "
                f"ошибок: {self.result.failed}"
            )
            app = self.app
            if isinstance(app, CloudflareManagerApp) and self.invalidate_cache:
                app.zone_cache = []
        except Exception as error:
            self.write_log(f"[red]Ошибка задания: {error}[/red]")
            self.query_one("#job-status", Static).update(f"Ошибка: {error}")
        finally:
            self.running = False
            self.query_one("#cancel-job", Button).disabled = True
            self.query_one("#close-job", Button).disabled = False
            self.query_one("#retry", Button).disabled = not bool(
                self.result and self.result.failed
            )

    @on(Button.Pressed)
    def handle_button(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-job":
            self.runner.cancel()
            event.button.disabled = True
        elif event.button.id == "close-job" and not self.running:
            self.dismiss(self.result)
        elif event.button.id == "retry" and self.result and not self.running:
            failed = sorted(
                {
                    item.domain
                    for item in self.result.results
                    if not item.success and item.message != "Отменено"
                }
            )
            if failed:
                self.start_job(failed)


class StartupAnalysisModal(ModalScreen[None]):
    FRAMES = (
        "        .--.        \n"
        "     .-(    ).      \n"
        "    (___.__)__)     \n"
        "       ╱│╲          ",
        "        .--.        \n"
        "    .- (    ) -.    \n"
        "   (___.____.___)   \n"
        "      ╱ │ ╲         ",
        "       .----.       \n"
        "    .-(      )-.    \n"
        "   (____.____.__)   \n"
        "     ╱  │  ╲        ",
    )

    def __init__(self, storage: Storage):
        super().__init__()
        self.storage = storage
        self.frame = 0

    def compose(self) -> ComposeResult:
        with Vertical(classes="startup-dialog"):
            yield Static(self.FRAMES[0], id="startup-animation")
            yield Label("АНАЛИЗ CLOUDFLARE", classes="startup-title")
            yield Static(
                "Подключение к аккаунтам и получение зон…", id="startup-status"
            )
            yield ProgressBar(
                total=100, show_percentage=True, show_eta=True, id="startup-progress"
            )
            yield Static("", id="startup-stats")

    def on_mount(self) -> None:
        self.set_interval(0.16, self.animate)
        self.run_worker(self.analyze(), exclusive=True, group="startup")

    def animate(self) -> None:
        self.frame = (self.frame + 1) % len(self.FRAMES)
        self.query_one("#startup-animation", Static).update(
            self.FRAMES[self.frame]
        )

    async def analyze(self) -> None:
        accounts = self.storage.load_accounts()
        progress_bar = self.query_one("#startup-progress", ProgressBar)
        status = self.query_one("#startup-status", Static)
        stats = self.query_one("#startup-stats", Static)
        progress_bar.update(total=max(1, len(accounts)), progress=0)
        errors = 0

        def update_progress(
            done: int,
            total: int,
            account_label: str,
            found: int,
            error: str | None,
        ) -> None:
            nonlocal errors
            errors += int(error is not None)
            percent = int((done / total) * 100) if total else 100
            progress_bar.update(total=max(1, total), progress=done)
            status.update(
                f"{percent:3d}%  Проверено {done}/{total}: {account_label}"
            )
            stats.update(
                f"Зон найдено: {found:,}    Ошибок API: {errors}"
            )

        try:
            zones = await list_all_zones(
                CloudflareClient(),
                accounts,
                concurrency=16,
                progress=update_progress,
            )
            app = self.app
            if isinstance(app, CloudflareManagerApp):
                app.zone_cache = zones
                app.analysis_complete = True
                zones_panel = app.query_one(ZoneTablePanel)
                zones_panel.zones = list(zones)
                zones_panel.refresh_teams()
                zones_panel.render_zones()
                app.query_one(DashboardPanel).refresh_stats()
            banned = sum(zone.phishing_detected for zone in zones)
            status.update("100%  Анализ Cloudflare завершён")
            stats.update(
                f"Зон: {len(zones):,}    Фишинг/бан: {banned}    "
                f"Ошибок API: {errors}"
            )
            self.set_timer(1.0, self.close_animation)
        except Exception as error:
            status.update(f"Ошибка анализа: {error}")
            self.set_timer(2.0, self.close_animation)

    def close_animation(self) -> None:
        self.dismiss(None)


class BasePanel(VerticalScroll):
    def __init__(self, storage: Storage, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.storage = storage

    def notice(self, title: str, message: str) -> None:
        self.app.push_screen(NoticeModal(title, message))


class DashboardPanel(BasePanel):
    def compose(self) -> ComposeResult:
        yield Label("Обзор", classes="panel-title")
        with Grid(id="stats-grid"):
            yield Static(id="stat-accounts", classes="stat-card")
            yield Static(id="stat-cnames", classes="stat-card")
            yield Static(id="stat-domains", classes="stat-card")
            yield Static(id="stat-cloudflare", classes="stat-card")
            yield Static(id="stat-banned", classes="stat-card")
            yield Static(id="stat-failed", classes="stat-card")
        yield Static(
            "Выберите раздел слева. Для навигации доступны F1–F8; "
            "Ctrl+R обновляет активный экран.",
            classes="hint-card",
        )

    def on_mount(self) -> None:
        self.refresh_stats()

    def refresh_stats(self) -> None:
        self.query_one("#stat-accounts", Static).update(
            f"[b]Аккаунты[/b]\n{len(self.storage.load_accounts())}"
        )
        self.query_one("#stat-cnames", Static).update(
            f"[b]CNAME[/b]\n{len(self.storage.load_cnames())}"
        )
        self.query_one("#stat-domains", Static).update(
            f"[b]Домены[/b]\n{len(self.storage.load_domains())}"
        )
        app = self.app
        zones = app.zone_cache if isinstance(app, CloudflareManagerApp) else []
        self.query_one("#stat-cloudflare", Static).update(
            f"[b]Зоны Cloudflare[/b]\n{len(zones):,}"
        )
        self.query_one("#stat-banned", Static).update(
            f"[b]Фишинг / бан[/b]\n{sum(zone.phishing_detected for zone in zones)}"
        )
        self.query_one("#stat-failed", Static).update(
            f"[b]Ошибки последнего задания[/b]\n"
            f"{len(self.storage.load_domains(self.storage.failed_path))}"
        )


class AccountsPanel(BasePanel):
    filtered_accounts: list[Account]

    def compose(self) -> ComposeResult:
        yield Label("Аккаунты Cloudflare", classes="panel-title")
        with Horizontal(classes="toolbar"):
            yield Input(placeholder="Фильтр по названию или email", id="accounts-filter")
            yield Button("Добавить", id="account-add", variant="success")
            yield Button("Изменить", id="account-edit")
            yield Button("Удалить", id="account-delete", variant="error")
        yield DataTable(id="accounts-table", zebra_stripes=True, cursor_type="row")

    def on_mount(self) -> None:
        self.query_one("#accounts-table", DataTable).add_columns(
            "Название", "Email", "API Key"
        )
        self.reload()

    def reload(self) -> None:
        needle = self.query_one("#accounts-filter", Input).value.lower()
        self.filtered_accounts = [
            account
            for account in self.storage.load_accounts()
            if needle in account.label.lower() or needle in account.email.lower()
        ]
        table = self.query_one("#accounts-table", DataTable)
        table.clear()
        for account in self.filtered_accounts:
            table.add_row(account.label, account.email, account.masked_key)

    def selected(self) -> Account | None:
        table = self.query_one("#accounts-table", DataTable)
        if (
            not self.filtered_accounts
            or table.cursor_row >= len(self.filtered_accounts)
        ):
            return None
        return self.filtered_accounts[table.cursor_row]

    @on(Input.Changed, "#accounts-filter")
    def filter_changed(self) -> None:
        self.reload()

    @on(Button.Pressed, "#account-add")
    def add_account(self) -> None:
        self.app.push_screen(AccountModal(), self._added)

    def _added(self, account: Account | None) -> None:
        if not account:
            return
        accounts = self.storage.load_accounts()
        if any(item.email.lower() == account.email.lower() for item in accounts):
            self.notice("Дубликат", "Аккаунт с таким email уже существует")
            return
        accounts.append(account)
        self.storage.save_accounts(accounts)
        self.reload()

    @on(Button.Pressed, "#account-edit")
    def edit_account(self) -> None:
        account = self.selected()
        if account:
            self.app.push_screen(AccountModal(account), self._edited)

    def _edited(self, updated: Account | None) -> None:
        original = self.selected()
        if not updated or not original:
            return
        accounts = self.storage.load_accounts()
        index = accounts.index(original)
        if any(
            item.email.lower() == updated.email.lower() and position != index
            for position, item in enumerate(accounts)
        ):
            self.notice("Дубликат", "Аккаунт с таким email уже существует")
            return
        accounts[index] = updated
        self.storage.save_accounts(accounts)
        self.reload()

    @on(Button.Pressed, "#account-delete")
    def delete_account(self) -> None:
        account = self.selected()
        if account:
            self.app.push_screen(
                ConfirmModal(
                    "Удалить аккаунт?",
                    f"{account.label}\n{account.email}\n\nAPI-ключ будет удалён локально.",
                    danger=True,
                ),
                lambda confirmed: self._delete_confirmed(account, confirmed),
            )

    def _delete_confirmed(self, account: Account, confirmed: bool) -> None:
        if confirmed:
            accounts = self.storage.load_accounts()
            accounts.remove(account)
            self.storage.save_accounts(accounts)
            self.reload()


class CnamesPanel(BasePanel):
    filtered_cnames: list[CnameTarget]

    def compose(self) -> ComposeResult:
        yield Label("CNAME targets", classes="panel-title")
        with Horizontal(classes="toolbar"):
            yield Input(placeholder="Фильтр", id="cnames-filter")
            yield Button("Добавить", id="cname-add", variant="success")
            yield Button("Изменить", id="cname-edit")
            yield Button("Удалить", id="cname-delete", variant="error")
        yield DataTable(id="cnames-table", zebra_stripes=True, cursor_type="row")

    def on_mount(self) -> None:
        self.query_one("#cnames-table", DataTable).add_columns("Название", "Target")
        self.reload()

    def reload(self) -> None:
        needle = self.query_one("#cnames-filter", Input).value.lower()
        self.filtered_cnames = [
            cname
            for cname in self.storage.load_cnames()
            if needle in cname.name.lower() or needle in cname.target.lower()
        ]
        table = self.query_one("#cnames-table", DataTable)
        table.clear()
        for cname in self.filtered_cnames:
            table.add_row(cname.name, cname.target)

    def selected(self) -> CnameTarget | None:
        table = self.query_one("#cnames-table", DataTable)
        if not self.filtered_cnames or table.cursor_row >= len(self.filtered_cnames):
            return None
        return self.filtered_cnames[table.cursor_row]

    @on(Input.Changed, "#cnames-filter")
    def filter_changed(self) -> None:
        self.reload()

    @on(Button.Pressed, "#cname-add")
    def add_cname(self) -> None:
        self.app.push_screen(CnameModal(), self._added)

    def _added(self, cname: CnameTarget | None) -> None:
        if not cname:
            return
        values = self.storage.load_cnames()
        values.append(cname)
        try:
            self.storage.save_cnames(values)
        except ValueError as error:
            self.notice("Не сохранено", str(error))
            return
        self.reload()

    @on(Button.Pressed, "#cname-edit")
    def edit_cname(self) -> None:
        cname = self.selected()
        if cname:
            self.app.push_screen(CnameModal(cname), self._edited)

    def _edited(self, updated: CnameTarget | None) -> None:
        original = self.selected()
        if not updated or not original:
            return
        values = self.storage.load_cnames()
        values[values.index(original)] = updated
        try:
            self.storage.save_cnames(values)
        except ValueError as error:
            self.notice("Не сохранено", str(error))
            return
        self.reload()

    @on(Button.Pressed, "#cname-delete")
    def delete_cname(self) -> None:
        cname = self.selected()
        if cname:
            self.app.push_screen(
                ConfirmModal(
                    "Удалить CNAME?", f"{cname.name}\n{cname.target}", danger=True
                ),
                lambda confirmed: self._delete_confirmed(cname, confirmed),
            )

    def _delete_confirmed(self, cname: CnameTarget, confirmed: bool) -> None:
        if confirmed:
            values = self.storage.load_cnames()
            values.remove(cname)
            self.storage.save_cnames(values)
            self.reload()


class DomainsPanel(BasePanel):
    def compose(self) -> ComposeResult:
        yield Label("Редактор доменов", classes="panel-title")
        with Horizontal(classes="toolbar"):
            yield Input(
                str(self.storage.domains_path), id="domain-import-path", placeholder="Путь к TXT"
            )
            yield Button("Импорт", id="domains-import")
            yield Button("Сохранить", id="domains-save", variant="success")
            yield Button("Очистить", id="domains-clear", variant="error")
        yield Static("", id="domains-count")
        yield TextArea(id="domains-editor", language=None, show_line_numbers=True)

    def on_mount(self) -> None:
        self.load_default()

    def load_default(self) -> None:
        domains = self.storage.load_domains()
        self.query_one("#domains-editor", TextArea).text = "\n".join(domains)
        self.update_count()

    def update_count(self) -> None:
        lines = self.query_one("#domains-editor", TextArea).text.splitlines()
        domains = unique_domains(lines)
        self.query_one("#domains-count", Static).update(
            f"Валидных уникальных доменов: {len(domains)}"
        )

    @on(TextArea.Changed, "#domains-editor")
    def editor_changed(self) -> None:
        self.update_count()

    @on(Button.Pressed, "#domains-save")
    def save(self) -> None:
        try:
            self.storage.save_domains(
                self.query_one("#domains-editor", TextArea).text.splitlines()
            )
            self.notice("Сохранено", f"Файл: {self.storage.domains_path}")
        except ValueError as error:
            self.notice("Ошибка", str(error))

    @on(Button.Pressed, "#domains-import")
    def import_file(self) -> None:
        path = self.query_one("#domain-import-path", Input).value.strip()
        domains = self.storage.load_domains(path)
        if not domains:
            self.notice("Импорт", "В файле не найдено валидных доменов")
            return
        current = self.query_one("#domains-editor", TextArea).text.splitlines()
        merged = unique_domains([*current, *domains])
        self.query_one("#domains-editor", TextArea).text = "\n".join(merged)

    @on(Button.Pressed, "#domains-clear")
    def clear(self) -> None:
        self.app.push_screen(
            ConfirmModal(
                "Очистить список?", "Все домены будут удалены из редактора.", danger=True
            ),
            lambda confirmed: self._clear_confirmed(confirmed),
        )

    def _clear_confirmed(self, confirmed: bool) -> None:
        if confirmed:
            self.query_one("#domains-editor", TextArea).text = ""


class AddZonePanel(BasePanel):
    def compose(self) -> ComposeResult:
        yield Label("Добавление зон", classes="panel-title")
        yield Static("1. Источник доменов", classes="step-title")
        with Horizontal(classes="form-row"):
            yield Input(str(self.storage.domains_path), id="add-source")
            yield Button("Загрузить", id="add-load")
        yield Static("Домены ещё не загружены", id="add-domain-count")
        yield Static("2. Аккаунт и CNAME", classes="step-title")
        with Grid(classes="form-grid"):
            yield Label("Аккаунт")
            yield Select([], id="add-account", prompt="Выберите аккаунт")
            yield Label("CNAME")
            yield Select([], id="add-cname", prompt="Выберите CNAME")
            yield Label("Параллельные операции")
            yield Input("10", id="add-concurrency", type="integer")
            yield Label("Без изменений в Cloudflare")
            yield Checkbox("Dry-run", id="add-dry-run")
        yield Static("3. Проверка и запуск", classes="step-title")
        yield Static("", id="add-summary", classes="hint-card")
        yield Button("Запустить AddZone", id="add-start", variant="success")

    def on_mount(self) -> None:
        self.domains: list[str] = []
        self.refresh_options()
        self.load_domains()

    def refresh_options(self) -> None:
        accounts = self.storage.load_accounts()
        cnames = self.storage.load_cnames()
        account_select = self.query_one("#add-account", Select)
        cname_select = self.query_one("#add-cname", Select)
        account_select.set_options(
            [(f"{item.label} — {item.email}", item.email) for item in accounts]
        )
        cname_select.set_options(
            [(f"{item.name} — {item.target}", item.target) for item in cnames]
        )

    def load_domains(self) -> None:
        source = self.query_one("#add-source", Input).value
        self.domains = self.storage.load_domains(source)
        self.query_one("#add-domain-count", Static).update(
            f"Загружено доменов: {len(self.domains)}"
        )
        self.update_summary()

    def update_summary(self) -> None:
        self.query_one("#add-summary", Static).update(
            f"Доменов: {len(getattr(self, 'domains', []))}\n"
            f"Операция: Add zone → SSL flexible → удалить старый DNS → CNAME → NS"
        )

    @on(Button.Pressed, "#add-load")
    def load_clicked(self) -> None:
        self.load_domains()

    @on(Button.Pressed, "#add-start")
    def start(self) -> None:
        if not self.domains:
            self.notice("Нет доменов", "Загрузите непустой TXT-файл")
            return
        account_value = self.query_one("#add-account", Select).value
        cname_value = self.query_one("#add-cname", Select).value
        if account_value is Select.NULL or cname_value is Select.NULL:
            self.notice("Не заполнено", "Выберите аккаунт и CNAME")
            return
        account = next(
            item for item in self.storage.load_accounts() if item.email == account_value
        )
        cname = str(cname_value)
        concurrency = max(
            1, int(self.query_one("#add-concurrency", Input).value or "10")
        )
        dry_run = self.query_one("#add-dry-run", Checkbox).value

        async def factory(
            retry_domains: list[str] | None,
            runner: JobRunner,
            callback: Callable[[JobProgress, JobResult], None],
        ) -> JobProgress:
            return await runner.add_domains(
                account,
                retry_domains or self.domains,
                cname,
                concurrency=concurrency,
                dry_run=dry_run,
                on_progress=callback,
            )

        def confirmed(ok: bool) -> None:
            if ok:
                self.app.push_screen(
                    JobModal(
                        self.storage,
                        f"AddZone — {account.label}",
                        len(self.domains),
                        factory,
                    )
                )

        self.app.push_screen(
            ConfirmModal(
                "Запустить добавление зон?",
                f"Доменов: {len(self.domains)}\nАккаунт: {account.label}\n"
                f"CNAME: {cname}\nПараллельно: {concurrency}\n"
                f"Dry-run: {'да' if dry_run else 'нет'}",
                danger=not dry_run,
            ),
            confirmed,
        )


class CleanerPanel(BasePanel):
    def compose(self) -> ComposeResult:
        yield Label("DNS Cleaner", classes="panel-title")
        with Horizontal(classes="form-row"):
            yield Input(str(self.storage.domains_path), id="clean-source")
            yield Button("Загрузить", id="clean-load")
        yield Static("", id="clean-count")
        yield Label("Аккаунты")
        yield SelectionList[str](id="clean-accounts")
        with Grid(classes="form-grid"):
            yield Label("Параллельные операции")
            yield Input("5", id="clean-concurrency", type="integer")
            yield Label("Удалить зоны после DNS")
            yield Checkbox("DNS + zone", id="clean-delete-zone")
            yield Label("Без изменений")
            yield Checkbox("Dry-run", id="clean-dry-run")
        yield Button("Запустить Cleaner", id="clean-start", variant="warning")

    def on_mount(self) -> None:
        selection = self.query_one("#clean-accounts", SelectionList)
        for account in self.storage.load_accounts():
            selection.add_option((f"{account.label} — {account.email}", account.email))
        self.load_domains()

    def load_domains(self) -> None:
        self.domains = self.storage.load_domains(
            self.query_one("#clean-source", Input).value
        )
        self.query_one("#clean-count", Static).update(
            f"Загружено доменов: {len(self.domains)}"
        )

    @on(Button.Pressed, "#clean-load")
    def load_clicked(self) -> None:
        self.load_domains()

    @on(Button.Pressed, "#clean-start")
    def start(self) -> None:
        if not self.domains:
            self.notice("Нет доменов", "Загрузите список доменов")
            return
        selected = set(self.query_one("#clean-accounts", SelectionList).selected)
        accounts = [
            item for item in self.storage.load_accounts() if item.email in selected
        ]
        if not accounts:
            self.notice("Нет аккаунтов", "Выберите хотя бы один аккаунт")
            return
        delete_zone = self.query_one("#clean-delete-zone", Checkbox).value
        dry_run = self.query_one("#clean-dry-run", Checkbox).value
        concurrency = max(
            1, int(self.query_one("#clean-concurrency", Input).value or "5")
        )

        async def factory(
            retry_domains: list[str] | None,
            runner: JobRunner,
            callback: Callable[[JobProgress, JobResult], None],
        ) -> JobProgress:
            return await runner.clean_domains(
                accounts,
                retry_domains or self.domains,
                delete_zone=delete_zone,
                concurrency=concurrency,
                dry_run=dry_run,
                on_progress=callback,
            )

        total = len(accounts) * len(self.domains)
        warning = (
            "Будут удалены ВСЕ DNS-записи и сами зоны."
            if delete_zone
            else "Будут удалены ВСЕ DNS-записи."
        )
        self.app.push_screen(
            ConfirmModal(
                "Подтвердите опасную операцию",
                f"{warning}\nАккаунтов: {len(accounts)}\nДоменов: {len(self.domains)}\n"
                f"Операций: {total}\nDry-run: {'да' if dry_run else 'нет'}",
                danger=not dry_run,
            ),
            lambda confirmed: confirmed
            and self.app.push_screen(
                JobModal(self.storage, "DNS Cleaner", total, factory)
            ),
        )


class NsHistoryPanel(BasePanel):
    def compose(self) -> ComposeResult:
        yield Label("История NS", classes="panel-title")
        with Horizontal(classes="toolbar"):
            yield Input(placeholder="Фильтр по домену или аккаунту", id="ns-filter")
            yield Button("Обновить", id="ns-refresh")
            yield Button("Экспорт", id="ns-export")
        yield DataTable(id="ns-table", zebra_stripes=True, cursor_type="row")

    def on_mount(self) -> None:
        self.query_one("#ns-table", DataTable).add_columns(
            "Аккаунт", "Домен", "Name servers"
        )
        self.reload()

    def reload(self) -> None:
        needle = self.query_one("#ns-filter", Input).value.lower()
        self.rows = [
            (label, domain, servers)
            for label, items in self.storage.load_ns_history().items()
            for domain, servers in items
            if needle in label.lower() or needle in domain.lower()
        ]
        table = self.query_one("#ns-table", DataTable)
        table.clear()
        for row in self.rows:
            table.add_row(*row)

    @on(Input.Changed, "#ns-filter")
    def filter_changed(self) -> None:
        self.reload()

    @on(Button.Pressed, "#ns-refresh")
    def refresh_clicked(self) -> None:
        self.reload()

    @on(Button.Pressed, "#ns-export")
    def export(self) -> None:
        path = self.storage.export_rows(
            "ns_export.txt",
            (f"{domain}\t{servers}\t[{label}]" for label, domain, servers in self.rows),
        )
        self.notice("Экспорт завершён", str(path))


class ZoneTablePanel(BasePanel):
    title = "Live-зоны Cloudflare"
    selection_id = "zones-accounts"
    table_id = "zones-table"
    filter_id = "zones-filter"

    def compose(self) -> ComposeResult:
        yield Label(self.title, classes="panel-title")
        with Horizontal(classes="toolbar"):
            yield Input(placeholder="Поиск домена", id=self.filter_id)
            yield Select([], id="zones-team", prompt="Все команды")
            yield Select(
                [
                    ("По домену", "domain"),
                    ("По CNAME", "cname"),
                    ("По команде", "team"),
                    ("По статусу", "status"),
                ],
                value="domain",
                allow_blank=False,
                id="zones-sort",
            )
            yield Checkbox("Только баны", id="zones-banned-only")
        with Horizontal(classes="toolbar"):
            yield Button("Обновить зоны", id="zones-refresh", variant="primary")
            yield Button("Загрузить CNAME команды", id="zones-load-cnames")
            yield Button("Экспорт", id="zones-export")
        with Horizontal(classes="toolbar"):
            yield Button("Отметить строку", id="zones-toggle")
            yield Button("Все баны", id="zones-select-banned", variant="warning")
            yield Button("Снять выбор", id="zones-clear-selection")
            yield Button("Удалить выбранные баны", id="zones-delete", variant="error")
        yield SelectionList[str](id=self.selection_id, classes="compact-selection")
        yield Static("", id="zones-status")
        yield ProgressBar(total=1, show_eta=True, id="zones-list-progress")
        yield DataTable(id=self.table_id, zebra_stripes=True, cursor_type="row")

    def on_mount(self) -> None:
        self.selected_zone_ids: set[tuple[str, str]] = set()
        selection = self.query_one(f"#{self.selection_id}", SelectionList)
        for account in self.storage.load_accounts():
            selection.add_option((account.label, account.email, True))
        table = self.query_one(f"#{self.table_id}", DataTable)
        table.clear(columns=True)
        if self.table_id == "search-table":
            table.add_columns(
                "Домен", "Статус", "Аккаунт", "Текущий CNAME", "Name servers"
            )
        else:
            table.add_columns(
                "✓",
                "Домен",
                "Текущий CNAME",
                "Статус",
                "Бан",
                "Команда",
                "Аккаунт",
                "Name servers",
            )
        app = self.app
        self.zones = list(app.zone_cache) if isinstance(app, CloudflareManagerApp) else []
        for progress_bar in self.query("#zones-list-progress"):
            progress_bar.display = False
        self.refresh_teams()
        self.render_zones()

    def refresh_teams(self) -> None:
        if not list(self.query("#zones-team")):
            return
        select = self.query_one("#zones-team", Select)
        current = select.value
        teams = sorted({zone.team for zone in self.zones})
        select.set_options([(team, team) for team in teams])
        if current in teams:
            select.value = current

    def selected_accounts(self) -> list[Account]:
        selected = set(
            self.query_one(f"#{self.selection_id}", SelectionList).selected
        )
        return [
            account
            for account in self.storage.load_accounts()
            if account.email in selected
        ]

    @on(Button.Pressed, "#zones-refresh")
    def refresh_clicked(self) -> None:
        accounts = self.selected_accounts()
        if not accounts:
            self.notice("Нет аккаунтов", "Выберите хотя бы один аккаунт")
            return
        self.fetch_zones(accounts)

    @work(exclusive=True, group="zones")
    async def fetch_zones(self, accounts: list[Account]) -> None:
        status = self.query_one("#zones-status", Static)
        status.update("Загрузка зон…")
        button = self.query_one("#zones-refresh", Button)
        progress_bar = self.query_one("#zones-list-progress", ProgressBar)
        button.disabled = True
        progress_bar.display = True
        progress_bar.update(total=len(accounts), progress=0)
        error_count = 0

        def update_account_progress(
            done: int,
            total: int,
            account_label: str,
            found: int,
            error: str | None,
        ) -> None:
            nonlocal error_count
            error_count += int(error is not None)
            progress_bar.update(total=total, progress=done)
            status.update(
                f"Проверено: {done}/{total}  Сейчас: {account_label}  "
                f"Зон: {found}  Ошибок: {error_count}"
            )

        try:
            client = CloudflareClient()
            self.zones = await list_all_zones(
                client,
                accounts,
                concurrency=16,
                progress=update_account_progress,
            )
            app = self.app
            if isinstance(app, CloudflareManagerApp):
                app.zone_cache = list(self.zones)
            status.update(
                f"Загружено зон: {len(self.zones)}; "
                f"аккаунтов: {len(accounts)}; ошибок: {error_count}"
            )
            self.refresh_teams()
            self.render_zones()
        finally:
            button.disabled = False

    def render_zones(self) -> None:
        needle = self.query_one(f"#{self.filter_id}", Input).value.lower()
        team_value = self.query_one("#zones-team", Select).value
        selected_team = None if team_value is Select.NULL else str(team_value)
        banned_only = self.query_one("#zones-banned-only", Checkbox).value
        self.visible_zones = [
            zone
            for zone in self.zones
            if needle in zone.name.lower() or needle in zone.account_label.lower()
            if selected_team is None or zone.team == selected_team
            if not banned_only or zone.phishing_detected
        ]
        sort_value = self.query_one("#zones-sort", Select).value
        if sort_value == "cname":
            self.visible_zones.sort(
                key=lambda zone: (
                    zone.current_cname.lower() if zone.current_cname else "\uffff",
                    zone.name,
                )
            )
        elif sort_value == "team":
            self.visible_zones.sort(key=lambda zone: (zone.team, zone.name))
        elif sort_value == "status":
            self.visible_zones.sort(key=lambda zone: (zone.status, zone.name))
        else:
            self.visible_zones.sort(key=lambda zone: zone.name)
        table = self.query_one(f"#{self.table_id}", DataTable)
        table.clear()
        for zone in self.visible_zones:
            identity = (zone.account_email, zone.zone_id)
            table.add_row(
                "☑" if identity in self.selected_zone_ids else "☐",
                zone.name,
                display_cname(zone),
                zone.status,
                "PHISHING" if zone.phishing_detected else "—",
                zone.team,
                zone.account_label,
                ", ".join(zone.name_servers),
            )
        banned = sum(zone.phishing_detected for zone in self.visible_zones)
        self.query_one("#zones-status", Static).update(
            f"Показано: {len(self.visible_zones):,}  "
            f"Банов: {banned}  Выбрано: {len(self.selected_zone_ids)}"
        )

    @on(Input.Changed)
    def filter_changed(self, event: Input.Changed) -> None:
        if event.input.id == self.filter_id:
            self.render_zones()

    @on(Select.Changed)
    def select_changed(self, event: Select.Changed) -> None:
        if event.select.id in {"zones-team", "zones-sort"}:
            self.render_zones()
        if event.select.id == "zones-team" and event.value is not Select.NULL:
            team = str(event.value)
            team_zones = [zone for zone in self.zones if zone.team == team]
            if team_zones and not all(zone.cname_loaded for zone in team_zones):
                self.load_team_cnames_clicked()

    @on(Checkbox.Changed, "#zones-banned-only")
    def banned_filter_changed(self) -> None:
        self.render_zones()

    def current_zone(self) -> Zone | None:
        table = self.query_one(f"#{self.table_id}", DataTable)
        if not self.visible_zones or table.cursor_row >= len(self.visible_zones):
            return None
        return self.visible_zones[table.cursor_row]

    @on(Button.Pressed, "#zones-toggle")
    def toggle_current(self) -> None:
        zone = self.current_zone()
        if not zone:
            return
        identity = (zone.account_email, zone.zone_id)
        if identity in self.selected_zone_ids:
            self.selected_zone_ids.remove(identity)
        else:
            self.selected_zone_ids.add(identity)
        self.render_zones()

    @on(DataTable.RowSelected, "#zones-table")
    def row_selected(self) -> None:
        self.toggle_current()

    @on(Button.Pressed, "#zones-select-banned")
    def select_all_banned(self) -> None:
        self.selected_zone_ids.update(
            (zone.account_email, zone.zone_id)
            for zone in self.visible_zones
            if zone.phishing_detected
        )
        self.render_zones()

    @on(Button.Pressed, "#zones-clear-selection")
    def clear_selection(self) -> None:
        self.selected_zone_ids.clear()
        self.render_zones()

    @on(Button.Pressed, "#zones-load-cnames")
    def load_team_cnames_clicked(self) -> None:
        team_value = self.query_one("#zones-team", Select).value
        if team_value is Select.NULL:
            self.notice(
                "Выберите команду",
                "CNAME загружаются для выбранной команды, чтобы не выполнять "
                "десятки тысяч лишних API-запросов.",
            )
            return
        team = str(team_value)
        zones = [zone for zone in self.zones if zone.team == team]
        if not zones:
            self.notice("Нет зон", f"У команды {team} нет загруженных зон")
            return
        if len(zones) > 1500:
            self.app.push_screen(
                ConfirmModal(
                    "Большая команда",
                    f"Для {len(zones):,} зон потребуется отдельный DNS-запрос. "
                    "Операция может занять некоторое время. Продолжить?",
                ),
                lambda confirmed: confirmed and self.load_team_cnames(zones),
            )
        else:
            self.load_team_cnames(zones)

    @work(exclusive=True, group="zone-cnames")
    async def load_team_cnames(self, zones: list[Zone]) -> None:
        status = self.query_one("#zones-status", Static)
        progress_bar = self.query_one("#zones-list-progress", ProgressBar)
        button = self.query_one("#zones-load-cnames", Button)
        button.disabled = True
        progress_bar.display = True
        progress_bar.update(total=len(zones), progress=0)
        errors = 0

        def update_progress(
            done: int,
            total: int,
            domain: str,
            found: int,
            error: str | None,
        ) -> None:
            nonlocal errors
            errors += int(error is not None)
            progress_bar.update(total=total, progress=done)
            status.update(
                f"CNAME: {done}/{total}  Сейчас: {domain}  "
                f"Найдено: {found}  Ошибок: {errors}"
            )

        try:
            updated, api_errors = await load_cnames_for_zones(
                CloudflareClient(),
                zones,
                self.storage.load_accounts(),
                concurrency=30,
                progress=update_progress,
            )
            replacements = {
                (zone.account_email, zone.zone_id): zone for zone in updated
            }
            self.zones = [
                replacements.get((zone.account_email, zone.zone_id), zone)
                for zone in self.zones
            ]
            app = self.app
            if isinstance(app, CloudflareManagerApp):
                app.zone_cache = list(self.zones)
            self.query_one("#zones-sort", Select).value = "cname"
            self.render_zones()
            status.update(
                f"CNAME загружены для {len(updated):,} зон; "
                f"ошибок: {len(api_errors)}"
            )
        finally:
            button.disabled = False

    @on(Button.Pressed, "#zones-delete")
    def delete_selected_banned(self) -> None:
        selected = [
            zone
            for zone in self.zones
            if (zone.account_email, zone.zone_id) in self.selected_zone_ids
            and zone.phishing_detected
        ]
        if not selected:
            self.notice(
                "Нет выбранных банов",
                "Отметьте строки с PHISHING или нажмите «Все баны». "
                "Обычные зоны этой кнопкой удалить нельзя.",
            )
            return
        preview = "\n".join(f"• {zone.name}" for zone in selected[:12])
        if len(selected) > 12:
            preview += f"\n• …ещё {len(selected) - 12}"
        self.app.push_screen(
            ConfirmModal(
                "Безвозвратно удалить заблокированные зоны?",
                f"Будет удалено зон: {len(selected)}\n\n{preview}\n\n"
                "Cloudflare не позволяет восстановить удалённую зону.",
                danger=True,
            ),
            lambda confirmed: confirmed and self._start_banned_deletion(selected),
        )

    def _start_banned_deletion(self, zones: list[Zone]) -> None:
        accounts = self.storage.load_accounts()
        by_email = {account.email: account for account in accounts}
        by_label = {account.label: account for account in accounts}
        items = [
            (account, zone)
            for zone in zones
            if (
                account := by_email.get(zone.account_email)
                or by_label.get(zone.account_label)
            )
        ]

        async def factory(
            retry_domains: list[str] | None,
            runner: JobRunner,
            callback: Callable[[JobProgress, JobResult], None],
        ) -> JobProgress:
            selected_items = (
                [item for item in items if item[1].name in set(retry_domains)]
                if retry_domains is not None
                else items
            )
            return await runner.delete_zones(
                selected_items, concurrency=8, on_progress=callback
            )

        self.app.push_screen(
            JobModal(
                self.storage,
                "Удаление заблокированных зон",
                len(items),
                factory,
                invalidate_cache=False,
            ),
            self._banned_deletion_finished,
        )

    def _banned_deletion_finished(self, result: JobProgress | None) -> None:
        if not result:
            return
        deleted = {
            (item.account, item.domain)
            for item in result.results
            if item.success
        }
        self.zones = [
            zone
            for zone in self.zones
            if (zone.account_label, zone.name) not in deleted
        ]
        self.selected_zone_ids.clear()
        app = self.app
        if isinstance(app, CloudflareManagerApp):
            app.zone_cache = list(self.zones)
        self.refresh_teams()
        self.render_zones()

    @on(Button.Pressed, "#zones-export")
    def export(self) -> None:
        path = self.storage.export_rows(
            "cloudflare_zones_export.txt",
            (
                f"{zone.name}\t{display_cname(zone)}\t{zone.status}\t"
                f"{'PHISHING' if zone.phishing_detected else '—'}\t"
                f"{zone.team}\t{zone.account_label}\t{', '.join(zone.name_servers)}"
                for zone in self.visible_zones
            ),
        )
        self.notice("Экспорт завершён", str(path))


class SearchPanel(ZoneTablePanel):
    title = "Поиск доменов"
    selection_id = "search-accounts"
    table_id = "search-table"
    filter_id = "search-filter"

    def compose(self) -> ComposeResult:
        yield Label(self.title, classes="panel-title")
        with Horizontal(classes="toolbar"):
            yield Input(
                placeholder="Домены через пробел, запятую или новую строку…",
                id=self.filter_id,
            )
            yield Button("Найти в Cloudflare", id="search-refresh", variant="primary")
        with Horizontal(classes="toolbar"):
            yield Select([], id="search-cname", prompt="Новый CNAME target")
            yield Input(
                placeholder="Введите новый CNAME, например target.example.com",
                id="search-new-cname",
            )
            yield Checkbox("Dry-run", id="search-cname-dry")
            yield Button(
                "Заменить CNAME найденных",
                id="search-replace-cname",
                variant="warning",
            )
        yield SelectionList[str](id=self.selection_id, classes="compact-selection")
        yield Static("", id="zones-status")
        yield ProgressBar(
            total=1, show_eta=True, id="search-account-progress"
        )
        yield DataTable(id=self.table_id, zebra_stripes=True, cursor_type="row")

    def on_mount(self) -> None:
        self.search_errors: list[str] = []
        super().on_mount()
        self.refresh_cnames()
        self.query_one("#search-account-progress", ProgressBar).display = False

    def refresh_cnames(self) -> None:
        self.query_one("#search-cname", Select).set_options(
            [
                (f"{item.name} — {item.target}", item.target)
                for item in self.storage.load_cnames()
            ]
        )

    @on(Select.Changed, "#search-cname")
    def preset_changed(self, event: Select.Changed) -> None:
        if event.value is not Select.NULL:
            self.query_one("#search-new-cname", Input).value = str(event.value)

    def search_terms(self) -> list[str]:
        value = self.query_one(f"#{self.filter_id}", Input).value.lower()
        return [item for item in re.split(r"[\s,;]+", value) if item]

    def render_zones(self) -> None:
        terms = self.search_terms()
        self.visible_zones = (
            [
                zone
                for zone in self.zones
                if any(term in zone.name.lower() for term in terms)
            ]
            if terms
            else []
        )
        table = self.query_one(f"#{self.table_id}", DataTable)
        table.clear()
        for zone in self.visible_zones:
            table.add_row(
                zone.name,
                zone.status,
                zone.account_label,
                display_cname(zone),
                ", ".join(zone.name_servers),
            )
        self.query_one("#zones-status", Static).update(
            f"Найдено: {len(self.visible_zones)}"
            + (
                f"  Ошибок аккаунтов: {len(self.search_errors)}"
                if self.search_errors
                else ""
            )
            if terms
            else "Введите один или несколько доменов"
        )

    @on(Button.Pressed, "#search-refresh")
    def refresh_clicked(self) -> None:
        self.start_direct_search()

    @on(Input.Submitted, "#search-filter")
    def search_submitted(self) -> None:
        self.start_direct_search()

    def start_direct_search(self) -> None:
        terms = self.search_terms()
        if not terms:
            self.notice("Нет запроса", "Введите полный домен или несколько доменов")
            return
        accounts = self.selected_accounts()
        if not accounts:
            self.notice("Нет аккаунтов", "Выберите хотя бы один аккаунт")
            return
        self.search_cloudflare(accounts, terms)

    @work(exclusive=True, group="zones")
    async def search_cloudflare(
        self, accounts: list[Account], domains: list[str]
    ) -> None:
        status = self.query_one("#zones-status", Static)
        button = self.query_one("#search-refresh", Button)
        progress_bar = self.query_one("#search-account-progress", ProgressBar)
        button.disabled = True
        progress_bar.display = True
        progress_bar.update(total=len(accounts), progress=0)
        status.update(
            f"Поиск {len(domains)} доменов в {len(accounts)} аккаунтах…"
        )

        def update_account_progress(
            done: int,
            total: int,
            account_label: str,
            found: int,
            error: str | None,
        ) -> None:
            progress_bar.update(total=total, progress=done)
            error_suffix = "  Ошибка API" if error else ""
            status.update(
                f"Проверено аккаунтов: {done}/{total}  "
                f"Сейчас: {account_label}  Найдено: {found}{error_suffix}"
            )

        try:
            client = CloudflareClient()
            self.zones, self.search_errors = await find_zones_across_accounts(
                client,
                accounts,
                domains,
                progress=update_account_progress,
            )
            app = self.app
            if isinstance(app, CloudflareManagerApp):
                merged = {
                    (zone.account_email, zone.zone_id): zone
                    for zone in app.zone_cache
                }
                merged.update(
                    {
                        (zone.account_email, zone.zone_id): zone
                        for zone in self.zones
                    }
                )
                app.zone_cache = list(merged.values())
            self.render_zones()
            if self.search_errors:
                status.update(
                    f"Найдено: {len(self.visible_zones)}. "
                    f"Ошибок авторизации/API: {len(self.search_errors)}. "
                    f"Первая ошибка: {self.search_errors[0]}"
                )
        finally:
            button.disabled = False

    @on(Button.Pressed, "#search-replace-cname")
    def replace_cnames(self) -> None:
        terms = self.search_terms()
        if not terms:
            self.notice("Нет запроса", "Введите домен или несколько доменов")
            return
        if not self.visible_zones:
            self.notice("Нет результатов", "Сначала обновите кэш и найдите домены")
            return
        target_value = self.query_one("#search-new-cname", Input).value.strip()
        if not target_value:
            self.notice("Не указан CNAME", "Введите новое направление CNAME")
            return
        try:
            target = CnameTarget.from_dict(
                {"name": target_value, "target": target_value}
            ).target
        except ValueError as error:
            self.notice("Некорректный CNAME", str(error))
            return
        dry_run = self.query_one("#search-cname-dry", Checkbox).value
        accounts = self.storage.load_accounts()
        by_email = {account.email: account for account in accounts}
        by_label = {account.label: account for account in accounts}
        items: list[tuple[Account, str]] = []
        seen: set[tuple[str, str]] = set()
        for zone in self.visible_zones:
            account = by_email.get(zone.account_email) or by_label.get(
                zone.account_label
            )
            if not account:
                continue
            identity = (account.email, zone.name)
            if identity not in seen:
                seen.add(identity)
                items.append((account, zone.name))
        if not items:
            self.notice("Нет аккаунтов", "Не удалось сопоставить зоны аккаунтам")
            return
        old_targets = sorted(
            {display_cname(zone) for zone in self.visible_zones}
        )
        old_preview = "\n".join(f"• {value}" for value in old_targets[:8])
        if len(old_targets) > 8:
            old_preview += f"\n• …ещё {len(old_targets) - 8}"

        async def factory(
            retry_domains: list[str] | None,
            runner: JobRunner,
            callback: Callable[[JobProgress, JobResult], None],
        ) -> JobProgress:
            selected_items = (
                [
                    item
                    for item in items
                    if item[1] in set(retry_domains)
                ]
                if retry_domains is not None
                else items
            )
            return await runner.replace_cnames(
                selected_items,
                target,
                concurrency=5,
                dry_run=dry_run,
                on_progress=callback,
            )

        def replacement_finished(result: JobProgress | None) -> None:
            if not result:
                return
            changed = {
                (item.account, item.domain)
                for item in result.results
                if item.success
            }
            self.zones = [
                replace(zone, current_cname=target, cname_loaded=True)
                if (zone.account_label, zone.name) in changed
                else zone
                for zone in self.zones
            ]
            app = self.app
            if isinstance(app, CloudflareManagerApp):
                app.zone_cache = [
                    replace(zone, current_cname=target, cname_loaded=True)
                    if (zone.account_label, zone.name) in changed
                    else zone
                    for zone in app.zone_cache
                ]
                live_panel = app.query_one("#zones", ZoneTablePanel)
                live_panel.zones = list(app.zone_cache)
                live_panel.render_zones()
            self.render_zones()

        self.app.push_screen(
            ConfirmModal(
                "Заменить apex CNAME?",
                f"Зон: {len(items)}\nТекущие направления:\n{old_preview}\n\n"
                f"Новое направление: {target}\n"
                "Будут заменены только apex-записи A/AAAA/CNAME; "
                "остальные DNS-записи сохранятся.\n"
                f"Dry-run: {'да' if dry_run else 'нет'}",
                danger=not dry_run,
            ),
            lambda confirmed: confirmed
            and self.app.push_screen(
                JobModal(
                    self.storage,
                    "Массовая замена CNAME",
                    len(items),
                    factory,
                    invalidate_cache=False,
                ),
                replacement_finished,
            ),
        )

    @on(DataTable.RowSelected)
    def show_details(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != self.table_id:
            return
        index = event.cursor_row
        if index >= len(self.visible_zones):
            return
        zone = self.visible_zones[index]
        self.notice(
            zone.name,
            f"Аккаунт: {zone.account_label}\nСтатус: {zone.status}\n"
            f"Текущий CNAME: {display_cname(zone)}\n"
            f"Zone ID: {zone.zone_id}\nNS: {', '.join(zone.name_servers) or '—'}",
        )


class CloudflareManagerApp(App[None]):
    TITLE = "DEXTER ONE — Cloudflare Manager"
    SUB_TITLE = "Безопасное управление зонами и DNS"
    BINDINGS = [
        ("ctrl+q", "quit", "Выход"),
        ("f1", "open_panel('add-zone')", "AddZone"),
        ("f2", "open_panel('cleaner')", "Cleaner"),
        ("f3", "open_panel('accounts')", "Аккаунты"),
        ("f4", "open_panel('cnames')", "CNAME"),
        ("f5", "open_panel('domains')", "Домены"),
        ("f6", "open_panel('ns-history')", "NS"),
        ("f7", "open_panel('zones')", "Зоны"),
        ("f8", "open_panel('search')", "Поиск"),
        ("ctrl+r", "refresh_current", "Обновить"),
    ]
    CSS = """
    Screen {
        background: #08111f;
        color: #dbeafe;
    }
    Header {
        background: #0b1830;
        color: #67e8f9;
    }
    Footer {
        background: #0b1830;
    }
    #app-body {
        height: 1fr;
    }
    #sidebar {
        width: 29;
        min-width: 24;
        background: #0b1830;
        border-right: solid #1e3a5f;
        padding: 1;
    }
    #brand {
        height: 4;
        content-align: center middle;
        text-align: center;
        color: #67e8f9;
        text-style: bold;
        border-bottom: solid #1e3a5f;
        margin-bottom: 1;
    }
    #navigation {
        height: 1fr;
        background: transparent;
    }
    ListItem {
        padding: 0 1;
        height: 3;
    }
    ListItem.--highlight {
        background: #164e63;
        color: white;
    }
    #content {
        width: 1fr;
        height: 1fr;
    }
    BasePanel {
        padding: 1 2;
    }
    .panel-title {
        height: 3;
        text-style: bold;
        color: #67e8f9;
        content-align: left middle;
        border-bottom: solid #1e3a5f;
        margin-bottom: 1;
    }
    .toolbar, .form-row {
        height: auto;
        min-height: 3;
        margin-bottom: 1;
    }
    .toolbar Input, .form-row Input {
        width: 1fr;
    }
    Button {
        margin-left: 1;
    }
    DataTable {
        height: 1fr;
        border: round #1e3a5f;
        background: #0a1526;
    }
    Input, Select, TextArea, SelectionList {
        border: round #1e3a5f;
        background: #0a1526;
    }
    TextArea {
        height: 1fr;
    }
    #stats-grid {
        grid-size: 3 2;
        grid-columns: 1fr 1fr 1fr;
        grid-rows: 7 7;
        grid-gutter: 1;
        height: 16;
    }
    .stat-card, .hint-card {
        border: round #155e75;
        background: #0a1b2e;
        padding: 1 2;
    }
    .stat-card {
        content-align: center middle;
        text-align: center;
    }
    .hint-card {
        height: auto;
        margin: 1 0;
    }
    .step-title {
        text-style: bold;
        color: #93c5fd;
        margin-top: 1;
    }
    .form-grid {
        grid-size: 2;
        grid-columns: 30 1fr;
        grid-rows: 4 4 4;
        height: auto;
    }
    .form-grid Label {
        content-align: left middle;
    }
    #add-start, #clean-start {
        width: 32;
        margin: 1 0;
    }
    #clean-accounts {
        height: 12;
    }
    .compact-selection {
        height: 7;
        margin-bottom: 1;
    }
    .dialog {
        width: 64;
        height: auto;
        max-height: 85%;
        align: center middle;
        background: #0b1830;
        border: thick #155e75;
        padding: 1 2;
    }
    .form-dialog {
        width: 72;
    }
    .dialog-title {
        height: 3;
        text-style: bold;
        color: #67e8f9;
        content-align: center middle;
    }
    .dialog-message {
        height: auto;
        min-height: 4;
        margin: 1 0;
    }
    .dialog-actions {
        height: 4;
        align-horizontal: right;
        margin-top: 1;
    }
    .error-text {
        color: #fca5a5;
        height: auto;
        min-height: 1;
    }
    JobModal {
        align: center middle;
    }
    StartupAnalysisModal {
        align: center middle;
        background: #030712 85%;
    }
    .startup-dialog {
        width: 82;
        height: 28;
        background: #071426;
        border: thick #22d3ee;
        padding: 1 4;
        content-align: center middle;
    }
    #startup-animation {
        height: 9;
        text-align: center;
        content-align: center middle;
        color: #67e8f9;
        text-style: bold;
    }
    .startup-title {
        height: 3;
        text-align: center;
        content-align: center middle;
        color: #a5f3fc;
        text-style: bold;
    }
    #startup-status, #startup-stats {
        height: 3;
        text-align: center;
        content-align: center middle;
    }
    #startup-progress {
        height: 3;
        margin: 1 0;
    }
    .job-dialog {
        width: 92%;
        height: 90%;
        background: #0b1830;
        border: thick #155e75;
        padding: 1 2;
    }
    #job-status {
        height: 2;
    }
    #job-progress {
        height: 2;
        margin-bottom: 1;
    }
    #search-account-progress, #zones-list-progress {
        height: 2;
        margin-bottom: 1;
    }
    .job-body {
        height: 1fr;
    }
    #job-log {
        width: 42%;
        border: round #1e3a5f;
        background: #050b14;
    }
    #job-results {
        width: 58%;
    }
    """

    def __init__(self, root: Path | str, *, auto_analyze: bool = True):
        super().__init__()
        self.storage = Storage(root)
        self.zone_cache: list[Zone] = []
        self.auto_analyze = auto_analyze
        self.analysis_complete = False

    def on_mount(self) -> None:
        if self.auto_analyze:
            self.push_screen(StartupAnalysisModal(self.storage))

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="app-body"):
            with Vertical(id="sidebar"):
                yield Static("DEXTER ONE\nCLOUDFLARE MANAGER", id="brand")
                yield ListView(
                    ListItem(Label("Обзор"), id="nav-dashboard"),
                    ListItem(Label("F1  Добавить зоны"), id="nav-add-zone"),
                    ListItem(Label("F2  DNS Cleaner"), id="nav-cleaner"),
                    ListItem(Label("F3  Аккаунты"), id="nav-accounts"),
                    ListItem(Label("F4  CNAME"), id="nav-cnames"),
                    ListItem(Label("F5  Домены"), id="nav-domains"),
                    ListItem(Label("F6  История NS"), id="nav-ns-history"),
                    ListItem(Label("F7  Live-зоны"), id="nav-zones"),
                    ListItem(Label("F8  Поиск"), id="nav-search"),
                    id="navigation",
                )
            with ContentSwitcher(initial="dashboard", id="content"):
                yield DashboardPanel(self.storage, id="dashboard")
                yield AddZonePanel(self.storage, id="add-zone")
                yield CleanerPanel(self.storage, id="cleaner")
                yield AccountsPanel(self.storage, id="accounts")
                yield CnamesPanel(self.storage, id="cnames")
                yield DomainsPanel(self.storage, id="domains")
                yield NsHistoryPanel(self.storage, id="ns-history")
                yield ZoneTablePanel(self.storage, id="zones")
                yield SearchPanel(self.storage, id="search")
        yield Footer()

    @on(ListView.Selected, "#navigation")
    def navigate(self, event: ListView.Selected) -> None:
        if event.item.id:
            self.action_open_panel(event.item.id.removeprefix("nav-"))

    def action_open_panel(self, panel: str) -> None:
        self.query_one("#content", ContentSwitcher).current = panel
        self.query_one(f"#{panel}").focus()
        if panel == "dashboard":
            self.query_one(DashboardPanel).refresh_stats()
        elif panel == "add-zone":
            self.query_one(AddZonePanel).refresh_options()
        elif panel == "search":
            self.query_one(SearchPanel).refresh_cnames()

    def action_refresh_current(self) -> None:
        panel = self.query_one("#content", ContentSwitcher).current
        if panel == "dashboard":
            self.query_one(DashboardPanel).refresh_stats()
        elif panel == "accounts":
            self.query_one(AccountsPanel).reload()
        elif panel == "cnames":
            self.query_one(CnamesPanel).reload()
        elif panel == "domains":
            self.query_one(DomainsPanel).load_default()
        elif panel == "ns-history":
            self.query_one(NsHistoryPanel).reload()
        elif panel == "zones":
            target = self.query_one("#zones", ZoneTablePanel)
            accounts = target.selected_accounts()
            if accounts:
                target.fetch_zones(accounts)
        elif panel == "search":
            self.query_one("#search", SearchPanel).start_direct_search()


def run_tui(root: Path | str) -> None:
    CloudflareManagerApp(root).run()
