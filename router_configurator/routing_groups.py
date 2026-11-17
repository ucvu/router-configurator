"""Service groups used by the cabinet's router settings."""
from pathlib import Path
from threading import Lock

from .generator import parse_geoip_list, parse_geosite_list


# Each row holds the ID, label, GeoSite tags and GeoIP tags.
SECTIONS = (
    ("Соцсети и мессенджеры", (
        ("instagram", "Instagram", ("INSTAGRAM",), ()),
        ("facebook", "Facebook", ("FACEBOOK", "FACEBOOK-DEV"), ("FACEBOOK",)),
        ("messenger", "Messenger", ("MESSENGER",), ()),
        ("threads", "Threads", ("THREADS",), ()),
        ("twitter", "X (Twitter)", ("TWITTER",), ("TWITTER",)),
        ("tiktok", "TikTok", ("TIKTOK",), ()),
        ("telegram", "Telegram", ("TELEGRAM",), ("TELEGRAM",)),
        ("whatsapp", "WhatsApp", ("WHATSAPP",), ()),
        ("discord", "Discord", ("DISCORD",), ()),
        ("discord-ip", "Discord — дополнительные IP-диапазоны", (), ()),
    )),
    ("Видео и музыка", (
        ("youtube", "YouTube", ("YOUTUBE",), ()),
        ("netflix", "Netflix", ("NETFLIX",), ("NETFLIX",)),
        ("tidal", "TIDAL", (), ()),
    )),
    ("Разработка", (
        ("openai", "ChatGPT и OpenAI API", ("OPENAI",), ()),
        ("anthropic", "Claude и Anthropic API", ("ANTHROPIC",), ()),
        ("gemini", "Google Gemini", ("GOOGLE-GEMINI",), ()),
        ("notebook", "Gemini Notebook (NotebookLM)", (), ()),
        ("flow", "Google Flow", (), ()),
        ("meta-ai", "Meta AI", (), ()),
        ("github", "GitHub", ("GITHUB",), ()),
        ("github-copilot", "GitHub Copilot", ("GITHUB-COPILOT",), ()),
        ("remotion", "Remotion", (), ()),
    )),
    ("Игры и устройства", (
        ("playstation", "PlayStation Network", ("PLAYSTATION",), ()),
        ("sony", "Sony", ("SONY",), ()),
        ("oculus", "Meta Quest / Oculus", ("OCULUS",), ()),
    )),
    ("Системы безопасности", (
        ("ajax", "Ajax Systems", (), ()),
    )),
)
GROUP_IDS = tuple(group_id for _, groups in SECTIONS for group_id, *_ in groups)
META_GROUPS = {"instagram", "facebook", "messenger", "threads", "whatsapp", "meta-ai", "oculus"}
META_SHARED = {"meta.com", "atmeta.com"}
EXTRA_DOMAINS = {
    "tidal": {
        "tidal.com",
        "listen.tidal.com",
        "api.tidal.com",
        "openapi.tidal.com",
        "auth.tidal.com",
        "login.tidal.com",
        "resources.tidal.com",
        "images.tidal.com",
        "amz-pr-cf.audio.tidal.com",
        "sp-ad-cf.audio.tidal.com",
        "sp-ad-fa.audio.tidal.com",
        "sp-pr-cf.audio.tidal.com",
        "sp-pr-fa.audio.tidal.com",
    },
    "facebook": {"tfbnw.net"},
    "tiktok": {"p16-tiktok-dm-sticker-sign-sg.ibyteimg.com"},
    "github": {"githubsupport.com"},
    "notebook": {"notebook.google", "notebook.google.com"},
    "flow": {"flow.google", "flow.google.com"},
    "meta-ai": {"meta.ai", "llama.com"},
}


def catalog():
    return {
        "sections": [
            {"title": title, "groups": [{"id": group_id, "label": label} for group_id, label, *_ in groups]}
            for title, groups in SECTIONS
        ],
        "default_groups": list(GROUP_IDS),
    }


def normalize_groups(selection):
    if not isinstance(selection, list) or not selection or len(selection) > len(GROUP_IDS):
        raise ValueError("Выберите хотя бы одну группу маршрутизации.")
    if any(not isinstance(group, str) or group not in GROUP_IDS for group in selection):
        raise ValueError("В списке есть неизвестная группа маршрутизации.")
    if len(set(selection)) != len(selection):
        raise ValueError("Группы маршрутизации не должны повторяться.")
    return [group for group in GROUP_IDS if group in selection]


def manual_entries(preset: Path):
    """Read the manually assigned Discord and Ajax ranges from the legacy preset."""
    entries = {"discord": set(), "ajax": set(), "remotion": set()}
    section = ""
    for raw_line in preset.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if line.startswith("#"):
            section = line[1:].strip().lower()
        elif section in entries and line.startswith("domain:"):
            entries[section].add(line.removeprefix("domain:").strip())
    if not all(entries.values()):
        raise ValueError("В стандартном пресете отсутствуют списки Discord или Ajax.")
    return entries


def _load_groups(project_dir: Path) -> dict[str, set[str]]:
    sites = parse_geosite_list((project_dir / ".cache" / "geosite.dat").read_bytes())
    networks = parse_geoip_list((project_dir / ".cache" / "geoip.dat").read_bytes())
    manual = manual_entries(project_dir / "preset-services-only.txt")
    groups = {}
    for _, section in SECTIONS:
        for group_id, _, site_tags, ip_tags in section:
            groups[group_id] = set().union(
                EXTRA_DOMAINS.get(group_id, ()),
                *(sites[tag] for tag in site_tags),
                *(networks[tag] for tag in ip_tags),
            )

    groups["discord-ip"] = manual["discord"]
    groups["ajax"] = manual["ajax"]
    groups["remotion"] = manual["remotion"]
    groups["notebook"].update(domain for domain in groups["gemini"] if "notebook" in domain)
    groups["gemini"].difference_update(groups["notebook"] | groups["flow"])
    groups["github"].difference_update(groups["github-copilot"])
    groups["sony"].difference_update(groups["playstation"])

    # Preserve the remaining addresses from the legacy Meta aggregate.
    meta_service_addresses = set().union(*(groups[group] for group in META_GROUPS))
    shared = META_SHARED | (set(sites["META"]) - meta_service_addresses)
    for group in META_GROUPS:
        groups[group].update(shared)

    return groups


_catalog_lock = Lock()
_cached_revision = None
_cached_groups = None


def load_groups(project_dir: Path) -> dict[str, set[str]]:
    global _cached_revision, _cached_groups
    files = (
        project_dir / ".cache" / "geosite.dat",
        project_dir / ".cache" / "geoip.dat",
        project_dir / "preset-services-only.txt",
    )
    with _catalog_lock:
        revision = tuple((file, file.stat().st_mtime_ns) for file in files)
        if revision != _cached_revision:
            _cached_groups = _load_groups(project_dir)
            _cached_revision = revision
        return _cached_groups


def compile_preset(project_dir: Path, selection: list[str]) -> str:
    selected = normalize_groups(selection)
    groups = load_groups(project_dir)
    lines = ["mode: proxy"]
    seen = set()
    for group in selected:
        addresses = groups[group]
        if not addresses:
            raise ValueError(f"Для группы {group} не найдено адресов.")
        lines.extend(f"domain:{address}" for address in sorted(addresses - seen))
        seen.update(addresses)
    return "\n".join(lines) + "\n"
