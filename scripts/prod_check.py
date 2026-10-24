"""Проверка прода после деплоя: гоняет 50+ ссылок через тот же downloader, что и бот.

Запуск на сервере (из папки проекта):
    docker compose exec -T bot python - < scripts/prod_check.py
    docker compose exec -T bot python - --download 0 < scripts/prod_check.py        # только get_info
    docker compose exec -T bot python - --limit 10 --download 3 < scripts/prod_check.py  # быстрый прогон

Telegram и БД не трогает — только yt-dlp (WARP-пул, прокси, PO-token, deno).
Алертов админу и рестартов WARP не будет: эти колбэки подключены только в процессе бота.
Запускать лучше, когда мало трафика: скрипт ходит через тот же WARP-пул, что и бот.
"""
import argparse
import asyncio
import logging
import os
import sys
import time
from collections import Counter
from importlib import metadata

# через `docker compose exec -T` терминала нет и Python копит вывод до конца прогона —
# включаем построчный вывод, чтобы прогресс было видно сразу
sys.stdout.reconfigure(line_buffering=True)


class _Collector(logging.Handler):
    """Собирает логи бота в список, чтобы не сорить в консоль и потом посчитать предупреждения."""

    def __init__(self):
        super().__init__(logging.INFO)
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record.getMessage())


# логи бота подключаем ДО импорта сервиса — он пишет в лог уже при создании
collector = _Collector()
_bot_logger = logging.getLogger("bot")
_bot_logger.addHandler(collector)
_bot_logger.setLevel(logging.INFO)
_bot_logger.propagate = False

from bot.services.youtube import FileTooLargeError, classify_error, downloader  # noqa: E402
from bot.utils.helpers import clean_youtube_url, is_youtube_url  # noqa: E402

# что ждём от ссылки:
#   ok            — должна нормально получить инфо (и скачаться)
#   live          — прямой эфир, бот должен его распознать
#   missing       — несуществующий ролик, ждём категорию unavailable
#   not_supported — формат ссылки, который бот пока не распознаёт (просто для информации)
LINKS = [
    # --- разные формы ссылок ---
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "ok", "Rick Astley — обычная ссылка"),
    ("https://youtu.be/hT_nvWreIhg?si=AbCdEfGh123", "ok", "Counting Stars — youtu.be с ?si="),
    ("https://m.youtube.com/watch?v=9bZkp7q19f0", "ok", "Gangnam Style — мобильная"),
    ("https://www.youtube.com/watch?v=kJQP7kiw5Fk&list=RDkJQP7kiw5Fk&start_radio=1", "ok", "Despacito — с плейлистом"),
    ("https://www.youtube.com/watch?v=JGwWNGJdvx8&t=42s", "ok", "Shape of You — с таймкодом"),
    ("https://youtube.com/shorts/jNQXAC9IVRw", "ok", "Me at the zoo — через /shorts/"),
    # --- высокие качества (4K/60fps) ---
    ("https://www.youtube.com/watch?v=aqz-KE-bpKQ", "ok", "Big Buck Bunny — 4K 60fps"),
    ("https://www.youtube.com/watch?v=LXb3EKWsInQ", "ok", "Costa Rica — 4K"),
    # --- популярные клипы ---
    ("https://www.youtube.com/watch?v=OPf0YbXqDm0", "ok", "Uptown Funk"),
    ("https://www.youtube.com/watch?v=RgKAFK5djSk", "ok", "See You Again"),
    ("https://www.youtube.com/watch?v=fJ9rUzIMcZQ", "ok", "Bohemian Rhapsody"),
    ("https://www.youtube.com/watch?v=CevxZvSJLk8", "ok", "Roar"),
    ("https://www.youtube.com/watch?v=60ItHLz5WEA", "ok", "Faded"),
    ("https://www.youtube.com/watch?v=YQHsXMglC9A", "ok", "Hello"),
    ("https://www.youtube.com/watch?v=pRpeEdMmmQ0", "ok", "Waka Waka"),
    ("https://www.youtube.com/watch?v=y6120QOlsfU", "ok", "Sandstorm"),
    ("https://www.youtube.com/watch?v=L_jWHffIx5E", "ok", "All Star"),
    ("https://www.youtube.com/watch?v=2Vv-BfVoq4g", "ok", "Perfect"),
    ("https://www.youtube.com/watch?v=lp-EO5I60KA", "ok", "Thinking Out Loud"),
    ("https://www.youtube.com/watch?v=e-ORhEE9VVg", "ok", "Blank Space"),
    ("https://www.youtube.com/watch?v=nfWlot6h_JM", "ok", "Shake It Off"),
    ("https://www.youtube.com/watch?v=QcIy9NiNbmo", "ok", "Bad Blood"),
    ("https://www.youtube.com/watch?v=09R8_2nJtjg", "ok", "Sugar"),
    ("https://www.youtube.com/watch?v=aJOTlE1K90k", "ok", "Girls Like You"),
    ("https://www.youtube.com/watch?v=SlPhMPnQ58k", "ok", "Memories"),
    ("https://www.youtube.com/watch?v=7PCkvCPvDXk", "ok", "All About That Bass"),
    ("https://www.youtube.com/watch?v=hLQl3WQQoQ0", "ok", "Someone Like You"),
    ("https://www.youtube.com/watch?v=rYEDA3JcQqw", "ok", "Rolling in the Deep"),
    ("https://www.youtube.com/watch?v=lWA2pjMjpBs", "ok", "Diamonds"),
    ("https://www.youtube.com/watch?v=KQ6zr6kCPj8", "ok", "Party Rock Anthem"),
    ("https://www.youtube.com/watch?v=uelHwf8o7_U", "ok", "Love The Way You Lie"),
    ("https://www.youtube.com/watch?v=1G4isv_Fylg", "ok", "Paradise"),
    ("https://www.youtube.com/watch?v=YykjpeuMNEk", "ok", "Hymn for the Weekend"),
    ("https://www.youtube.com/watch?v=dvgZkm1xWPE", "ok", "Viva La Vida"),
    ("https://www.youtube.com/watch?v=k4V3Mo61fJM", "ok", "Fix You"),
    ("https://www.youtube.com/watch?v=hTWKbfoikeg", "ok", "Smells Like Teen Spirit"),
    ("https://www.youtube.com/watch?v=1w7OgIMMRc4", "ok", "Sweet Child O' Mine"),
    ("https://www.youtube.com/watch?v=btPJPFnesV4", "ok", "Eye of the Tiger"),
    ("https://www.youtube.com/watch?v=djV11Xbc914", "ok", "Take On Me"),
    ("https://www.youtube.com/watch?v=Zi_XLOBDo_Y", "ok", "Billie Jean"),
    ("https://www.youtube.com/watch?v=sOnqjkJTMaA", "ok", "Thriller — длинный клип"),
    ("https://www.youtube.com/watch?v=4NRXx6U8ABQ", "ok", "Blinding Lights"),
    ("https://www.youtube.com/watch?v=XqZsoesa55w", "ok", "Baby Shark"),
    ("https://www.youtube.com/watch?v=ktvTqknDobU", "ok", "Radioactive"),
    ("https://www.youtube.com/watch?v=7wtfhZwyrcc", "ok", "Believer"),
    ("https://www.youtube.com/watch?v=fKopy74weus", "ok", "Thunder"),
    ("https://www.youtube.com/watch?v=pXRviuL6vMY", "ok", "Stressed Out"),
    ("https://www.youtube.com/watch?v=450p7goxZqg", "ok", "All of Me"),
    ("https://www.youtube.com/watch?v=JRfuAukYTKg", "ok", "Titanium"),
    ("https://www.youtube.com/watch?v=IcrbM1l_BoI", "ok", "Wake Me Up"),
    ("https://www.youtube.com/watch?v=5NV6Rdv1a3I", "ok", "Get Lucky"),
    ("https://www.youtube.com/watch?v=tAGnKpE4NCI", "ok", "Nothing Else Matters"),
    ("https://www.youtube.com/watch?v=v2AC41dglnM", "ok", "Thunderstruck"),
    ("https://www.youtube.com/watch?v=eVTXPUF4Oz4", "ok", "In The End"),
    ("https://www.youtube.com/watch?v=kXYiU_JCYtU", "ok", "Numb"),
    ("https://www.youtube.com/watch?v=ZZ5LpwO-An4", "ok", "HEYYEYAAEYAAAEYAEYAA"),
    # --- особые случаи ---
    ("https://www.youtube.com/live/jfKfPfyJRdk", "live", "Lofi Girl — круглосуточный эфир"),
    ("https://youtu.be/aaaaaaaaaaa", "missing", "несуществующий ролик"),
    ("https://www.youtube.com/watch?v=00000000000", "missing", "несуществующий ролик"),
    ("https://music.youtube.com/watch?v=fJ9rUzIMcZQ", "not_supported", "YouTube Music"),
    ("https://www.youtube.com/embed/dQw4w9WgXcQ", "not_supported", "embed-ссылка"),
]

# ошибки на стороне ролика (удалён, гео-блок, возраст) — бот не виноват, ссылку просто заменить
VIDEO_SIDE = {"unavailable", "geo_blocked", "age_restricted"}

# так _parse_qualities отвечает, когда форматов нет вообще. get_info при этом считается
# успешным (ignore_no_formats_error) — за заглушкой прячется бот-детект или удалённый ролик
STUB_QUALITIES = {"360": 0, "720": 0}

# формулировки, которых нет в classify_error бота, но они тоже значат «ролика нет»
_EXTRA_UNAVAILABLE = ("this video is unavailable",)

# категории, которые объясняют заглушку лучше, чем общее «форматов нет»
_STRONG_REASONS = VIDEO_SIDE | {"ip_blocked", "soft_block", "expired_url"}

INFO_TIMEOUT = 120      # сек на один get_info
DOWNLOAD_TIMEOUT = 600  # сек на одно скачивание
MAX_DOWNLOAD_DURATION = 600  # для теста скачиваем только ролики до 10 минут

# предупреждения, по которым видно, что деплой встал криво
WARNING_PATTERNS = {
    "n challenge solving failed": "n-challenge не решён (deno / yt-dlp-ejs)",
    "Signature solving failed": "подпись не решена (deno / yt-dlp-ejs)",
    "Remote components challenge solver": "нет пакета yt-dlp-ejs",
    "different versions": "плагин bgutil и сервер bgutil разных версий",
    "Missing required Visitor Data": "GVS PO-token не выдан",
    "not a bot": "бот-детект YouTube",
    "HTTP Error 429": "429 Too Many Requests",
    "HTTP Error 403": "403 Forbidden",
}


def _metric_source(records: list[str], prefix: str, url: str | None = None) -> str:
    """Достаёт source=... из строки [METRIC] (последней подходящей)."""
    for msg in reversed(records):
        if not msg.startswith(prefix):
            continue
        if url and not msg.endswith(f"url={url}"):
            continue
        for part in msg.split():
            if part.startswith("source="):
                return part.removeprefix("source=")
    return "?"


def _reason(messages: list[str]) -> str:
    """Категория по ошибке или предупреждениям yt-dlp за один get_info."""
    cats = []
    for msg in messages:
        if any(p in msg.lower() for p in _EXTRA_UNAVAILABLE):
            cats.append("unavailable")
        else:
            cats.append(classify_error(msg))
    for cat in cats:
        if cat in _STRONG_REASONS:
            return cat
    return "no_formats" if "no_formats" in cats else "unknown"


def _short(text: str, n: int = 110) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


# ---------- шаг 1: окружение ----------

async def check_environment() -> list[str]:
    """Версии пакетов + доступность PO-token и deno. Возвращает список проблем."""
    problems = []
    print("=== Окружение ===")
    for pkg in ("yt-dlp", "yt-dlp-ejs", "bgutil-ytdlp-pot-provider"):
        try:
            print(f"  {pkg:28} {metadata.version(pkg)}")
        except metadata.PackageNotFoundError:
            print(f"  {pkg:28} НЕ УСТАНОВЛЕН")
            problems.append(f"не установлен {pkg}")

    # yt-dlp грузит плагины (bgutil) при первом создании YoutubeDL. Если это случится
    # сразу в 4 потоках — гонка и "PoTokenProvider already registered". Грузим заранее, в одном
    import yt_dlp
    yt_dlp.YoutubeDL({"quiet": True, "logger": logging.getLogger("bot.prod_check")})

    start = len(collector.records)
    await downloader.check_pot_provider()
    await downloader.check_js_runtime()
    for msg in collector.records[start:]:
        print(f"  {msg}")
        if "НЕдоступен" in msg or "НЕ НАЙДЕН" in msg or "не отвечает" in msg:
            problems.append(_short(msg, 80))
    print()
    return problems


# ---------- шаг 2: get_info по всем ссылкам ----------

async def check_info(url: str, kind: str, note: str, sem: asyncio.Semaphore) -> dict:
    """Прогоняет ссылку так же, как хендлер: распознать → очистить → get_info."""
    row = {"url": url, "kind": kind, "note": note, "sec": 0.0}
    if not is_youtube_url(url):
        row["status"] = "not_recognized"
        return row

    clean = clean_youtube_url(url)
    row["clean"] = clean
    async with sem:
        t0 = time.monotonic()
        try:
            info = await asyncio.wait_for(downloader.get_info(clean), INFO_TIMEOUT)
        except asyncio.TimeoutError:
            row.update(status="error", category="timeout", error=f"таймаут {INFO_TIMEOUT}с")
        except Exception as e:
            row.update(status="error", category=_reason([str(e)]), error=str(e))
        else:
            row.update(
                title=info.title,
                duration=info.duration or 0,
                qualities=info.qualities or {},
                source=_metric_source(collector.records, "[METRIC] get_info", clean),
            )
            if info.is_live:
                row["status"] = "live"
            elif row["qualities"] == STUB_QUALITIES:
                row["status"] = "stub"
            else:
                row["status"] = "ok"
        row["sec"] = time.monotonic() - t0
    return row


def verdict(row: dict) -> str:
    """pass / fail / skip / info — сравниваем результат с тем, что ждали."""
    kind, status = row["kind"], row["status"]
    if kind == "not_supported":
        return "info"
    if kind == "live":
        return "pass" if status == "live" else "fail"
    if kind == "missing":
        return "pass" if row.get("category") == "unavailable" else "fail"
    # kind == ok
    if status == "ok":
        return "pass"
    if row.get("category") in VIDEO_SIDE:
        return "skip"
    return "fail"


def print_info_row(i: int, total: int, row: dict) -> None:
    v = verdict(row)
    mark = {"pass": "OK  ", "fail": "FAIL", "skip": "SKIP", "info": "INFO"}[v]
    if row["status"] == "stub":
        mark = "STUB"  # причину уточним вторым проходом
    head = f"[{i:2}/{total}] {mark} {row['sec']:5.1f}с"
    status = row["status"]
    if status == "ok":
        q = ",".join(sorted(row["qualities"], key=int))
        print(f"{head} {row['source']:6} {q:22} {_short(row['title'], 45)}")
    elif status == "live":
        print(f"{head} эфир распознан — {row['note']}")
    elif status == "stub":
        print(f"{head} форматы не получены (заглушка 360/720) — {row['note']}")
    elif status == "not_recognized":
        print(f"{head} бот не распознаёт ссылку — {row['note']}")
    else:
        print(f"{head} [{row['category']}] {row['note']}: {_short(row['error'], 90)}")


async def explain_stubs(rows: list[dict]) -> None:
    """Повторяет get_info для заглушек по одному — так все предупреждения yt-dlp
    точно относятся к этой ссылке (у многих из них нет ID ролика).
    """
    stubs = [r for r in rows if r["status"] == "stub"]
    if not stubs:
        return
    print(f"\n=== Причины заглушек ({len(stubs)} шт., по одному) ===")
    for row in stubs:
        start = len(collector.records)
        extra = []
        info = None
        try:
            info = await asyncio.wait_for(downloader.get_info(row["clean"]), INFO_TIMEOUT)
        except Exception as e:
            extra.append(str(e))

        if info and info.is_live:
            row["status"] = "live"
            print(f"  со второго раза: эфир — {row['note']}")
        elif info and info.qualities and info.qualities != STUB_QUALITIES:
            row.update(status="ok", qualities=info.qualities, title=info.title,
                       duration=info.duration or 0, source="retry", flaky=True)
            print(f"  со второго раза получилось — {row['note']}")
        else:
            row["category"] = _reason(collector.records[start:] + extra)
            print(f"  [{row['category']}] {row['note']}")


# ---------- шаг 3: реальные скачивания ----------

async def check_downloads(rows: list[dict], count: int) -> list[dict]:
    """Скачивает несколько роликов по очереди: аудио / 360 / 720 вперемешку.
    По одному — чтобы точно знать source каждого и не грузить прод.
    """
    candidates = [
        r for r in rows
        if r["status"] == "ok" and 0 < r["duration"] <= MAX_DOWNLOAD_DURATION
    ][:count]
    if not candidates:
        return []

    print(f"\n=== Скачивание ({len(candidates)} шт., по одному) ===")
    cycle = ["audio", "360", "720"]
    results = []
    for i, row in enumerate(candidates):
        fmt = cycle[i % len(cycle)]
        if fmt != "audio" and fmt not in row["qualities"]:
            fmt = min(row["qualities"], key=int)  # нужного качества нет — берём самое низкое

        res_row = {"note": row["note"], "fmt": fmt}
        start = len(collector.records)
        t0 = time.monotonic()
        result = None
        try:
            if fmt == "audio":
                coro = downloader.download_audio(row["clean"])
            else:
                coro = downloader.download_video(row["clean"], fmt)
            result = await asyncio.wait_for(coro, DOWNLOAD_TIMEOUT)
            size_mb = os.path.getsize(result.file_path) / 1024 / 1024
            res_row.update(
                ok=True,
                size_mb=size_mb,
                source=_metric_source(collector.records[start:], "[METRIC] download_"),
                res=f"{result.width}x{result.height}" if result.width else "—",
                thumb=bool(result.thumb_path),
            )
        except FileTooLargeError as e:
            res_row.update(ok=True, too_large=True, error=str(e))
        except asyncio.TimeoutError:
            res_row.update(ok=False, category="timeout", error=f"таймаут {DOWNLOAD_TIMEOUT}с")
        except Exception as e:
            category = classify_error(str(e))
            # ролик удалён/заблокирован правообладателем — бот не виноват, это не сбой
            res_row.update(ok=category in VIDEO_SIDE, skipped=category in VIDEO_SIDE,
                           category=category, error=str(e))
        finally:
            if result:
                downloader.cleanup(result)
        res_row["sec"] = time.monotonic() - t0

        head = f"[{i + 1:2}/{len(candidates)}] {'OK  ' if res_row['ok'] else 'FAIL'} {res_row['sec']:5.1f}с {fmt:5}"
        if res_row.get("skipped"):
            head = head.replace("OK  ", "SKIP")
            print(f"{head} [{res_row['category']}] ролик недоступен — {row['note']}: {_short(res_row['error'], 60)}")
        elif res_row.get("too_large"):
            print(f"{head} слишком большой (сработал FileTooLargeError) — {row['note']}")
        elif res_row["ok"]:
            thumb = "превью есть" if res_row["thumb"] else "БЕЗ превью"
            print(
                f"{head} {res_row['source']:13} {res_row['size_mb']:6.1f} МБ "
                f"{res_row['res']:10} {thumb:11} {_short(row['note'], 30)}"
            )
        else:
            print(f"{head} [{res_row['category']}] {row['note']}: {_short(res_row['error'], 80)}")
        results.append(res_row)
    return results


# ---------- итог ----------

def print_summary(rows: list[dict], downloads: list[dict], env_problems: list[str]) -> bool:
    """Печатает сводку. Возвращает True, если прод в порядке."""
    verdicts = Counter(verdict(r) for r in rows)
    ok_rows = [r for r in rows if r["kind"] == "ok"]
    ok_pass = sum(1 for r in ok_rows if verdict(r) == "pass")
    ok_tested = sum(1 for r in ok_rows if verdict(r) != "skip")
    info_times = [r["sec"] for r in ok_rows if r["status"] == "ok"]
    sources = Counter(r["source"] for r in ok_rows if r["status"] == "ok")
    fail_cats = Counter(
        r.get("category", r["status"]) for r in rows if verdict(r) == "fail"
    )

    print("\n=== Итог ===")
    print(f"get_info: {ok_pass}/{ok_tested} обычных ссылок прошли", end="")
    if info_times:
        print(f", среднее {sum(info_times) / len(info_times):.1f}с, источники: {dict(sources)}")
    else:
        print()
    print(f"проверки: прошло {verdicts['pass']}, упало {verdicts['fail']}, "
          f"пропущено (ролик недоступен) {verdicts['skip']}, инфо {verdicts['info']}")
    if fail_cats:
        print(f"причины падений: {dict(fail_cats)}")

    skipped = [r["note"] for r in rows if verdict(r) == "skip"]
    if skipped:
        print(f"замени эти ссылки (ролик удалён/закрыт): {', '.join(skipped)}")
    unsupported = [r["note"] for r in rows if r["status"] == "not_recognized"]
    if unsupported:
        print(f"бот не распознаёт: {', '.join(unsupported)}")
    flaky = [r["note"] for r in rows if r.get("flaky")]
    if flaky:
        print(f"получилось только со второго раза: {', '.join(flaky)}")
    stub_missing = sum(1 for r in rows if r["kind"] == "missing" and r["status"] == "stub")
    if stub_missing:
        print(f"несуществующих роликов, которые get_info принял за нормальные: {stub_missing} "
              f"(юзер увидит кнопки 360/720, ошибка будет только при скачивании — известная проблема)")

    dl_fail = 0
    if downloads:
        dl_skip = sum(1 for d in downloads if d.get("skipped"))
        dl_ok = sum(1 for d in downloads if d["ok"]) - dl_skip
        dl_fail = len(downloads) - dl_ok - dl_skip
        no_thumb = sum(1 for d in downloads if d.get("size_mb") and not d["thumb"])
        speeds = [d["size_mb"] / d["sec"] for d in downloads if d.get("size_mb") and d["sec"] > 0]
        avg_speed = f", средняя скорость {sum(speeds) / len(speeds):.1f} МБ/с" if speeds else ""
        skip_txt = f", пропущено (ролик недоступен): {dl_skip}" if dl_skip else ""
        print(f"скачивание: {dl_ok}/{len(downloads) - dl_skip} успешно{avg_speed}, "
              f"без превью: {no_thumb}{skip_txt}")

    # предупреждения yt-dlp за весь прогон
    warn_counts = {
        label: sum(1 for m in collector.records if pattern in m)
        for pattern, label in WARNING_PATTERNS.items()
    }
    fallbacks = sum(1 for m in collector.records if "не дал инфо" in m or "не сработал" in m)
    empty = sum(1 for m in collector.records if "дал инфо без форматов" in m)
    print("\nпредупреждения yt-dlp за прогон:")
    for label, n in warn_counts.items():
        print(f"  {n:4}  {label}")
    print(f"  {fallbacks:4}  попыток упало с ошибкой и ушло в fallback")
    print(f"  {empty:4}  ответов get_info без форматов (ушли на следующий источник)")

    # вердикт
    deploy_broken = (
        env_problems
        or warn_counts["нет пакета yt-dlp-ejs"] > 0
        or warn_counts["плагин bgutil и сервер bgutil разных версий"] > 0
    )
    info_fail_rate = 1 - ok_pass / ok_tested if ok_tested else 1
    other_fails = verdicts["fail"] - (ok_tested - ok_pass)  # live/missing повели себя не так

    print()
    if env_problems:
        print("проблемы окружения: " + "; ".join(env_problems))
    if deploy_broken:
        print("❌ Деплой встал не полностью — см. окружение и предупреждения выше")
        return False
    if info_fail_rate <= 0.05 and dl_fail == 0 and other_fails == 0:
        print("✅ Прод работает нормально")
        return True
    if info_fail_rate <= 0.15:
        print(f"⚠️  Работает, но есть сбои: get_info падает в {info_fail_rate:.0%} случаев, "
              f"скачиваний упало {dl_fail}")
        return True
    print(f"❌ Много сбоев: get_info падает в {info_fail_rate:.0%} случаев")
    return False


async def main(args: argparse.Namespace) -> bool:
    links = LINKS[: args.limit] if args.limit else LINKS
    t_start = time.monotonic()

    env_problems = await check_environment()

    print(f"=== get_info ({len(links)} ссылок, параллельно по {args.workers}) ===")
    sem = asyncio.Semaphore(args.workers)
    tasks = [asyncio.create_task(check_info(u, k, n, sem)) for u, k, n in links]
    rows = []
    for i, fut in enumerate(asyncio.as_completed(tasks), 1):
        row = await fut
        print_info_row(i, len(links), row)
        rows.append(row)

    await explain_stubs(rows)
    downloads = await check_downloads(rows, args.download) if args.download else []
    ok = print_summary(rows, downloads, env_problems)
    print(f"\nвремя прогона: {time.monotonic() - t_start:.0f}с")
    return ok


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Проверка прода YouTube-бота после деплоя")
    parser.add_argument("--download", type=int, default=10,
                        help="сколько роликов реально скачать (0 — только get_info), по умолчанию 10")
    parser.add_argument("--workers", type=int, default=4,
                        help="сколько get_info параллельно, по умолчанию 4")
    parser.add_argument("--limit", type=int, default=0,
                        help="взять только первые N ссылок (для быстрого прогона)")
    ok = asyncio.run(main(parser.parse_args()))
    raise SystemExit(0 if ok else 1)
