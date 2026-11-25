import re
from dataclasses import dataclass
from datetime import date

# Accept complete statements in the supported operator grammar. Partial matches,
# quoted commands, conditions and alternatives cannot authorize a write.
_ORDER = r"(?:[0-9]{5,}|[a-z][a-z0-9]*(?:-[a-z0-9]+)*-[a-z0-9]{3,})"
_WRITE = re.compile(
    rf"(?:пожалуйста,?\s+)?(?:перенеси(?:те)?|прошу перенести|хочу перенести)"
    rf"\s+(?:доставку\s+заказа|заказ)\s+(?P<order>{_ORDER})(?:\s+ещё раз)?\s+на\s+(?P<date>.+?)[.!]?",
    re.IGNORECASE,
)
_READ = re.compile(
    rf"(?:(?:покажи(?:те)?|проверь(?:те)?)\s+заказ|можно ли перенести заказ)\s+(?P<order>{_ORDER})[.!?]?",
    re.IGNORECASE,
)
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
_SHORT = re.compile(r"(?P<day>\d{1,2})[.\/](?P<month>\d{1,2})", re.ASCII)
_MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
_WORDS = re.compile(
    rf"(?P<day>[0-9]{{1,2}}) (?P<month>{'|'.join(_MONTHS)})"
    r"(?: (?P<year>[0-9]{4})(?: года)?)?"
)


@dataclass(frozen=True, slots=True)
class RequestIntent:
    order_id: str
    requested_date: date | None
    requires_unique_option: bool = False


def _business_date(value: str, reference_year: int | None) -> tuple[date, bool] | None:
    try:
        if _ISO.fullmatch(value):
            return date.fromisoformat(value), False
        words = _WORDS.fullmatch(value)
        short = _SHORT.fullmatch(value)
        if words:
            year = words["year"]
            month = _MONTHS.index(words["month"]) + 1
            day = int(words["day"])
        elif short:
            year = None
            month = int(short["month"])
            day = int(short["day"])
        else:
            return None
        if year is None and reference_year is None:
            return None
        return date(
            int(year) if year is not None else (reference_year or 0), month, day
        ), (year is None)
    except ValueError:
        return None


def resolve_intent(text: str, reference_year: int | None) -> RequestIntent | None:
    if len(text) > 512:
        return None
    text = text.strip()
    read = _READ.fullmatch(text)
    if read:
        return RequestIntent(read["order"], None)
    write = _WRITE.fullmatch(text)
    if write:
        parsed = _business_date(write["date"].lower(), reference_year)
        if parsed:
            return RequestIntent(write["order"], *parsed)
    return None
