"""Выписка банка — что на самом деле прошло по счёту.

Сверка двух баз 1С сравнивает их между собой: платёж, который не внесли ни
в одну, и платёж, внесённый в обе на другого получателя, она не видит.
Выписка — третий источник: каждая операция по счёту должна быть и в
управленке, и в налоговой.

Формат — PDF-выписка Мбанка «Выписка по счету …»: шапка (счёт, банк,
организация с ИНН, период, входящий остаток), дальше таблица «Документ /
Дата операции / Корреспондент / Оборот Дт / Оборот Кт / Назначение
платежа», в конце «ИТОГО» и исходящий остаток. Оборот Дт — деньги ушли со
счёта, Кт — пришли.

QR-оплаты (назначение начинается с «QR/») в выписке тоже есть, но их
сверяет QR-отчёт банка поштучно, со временем оплаты, — здесь они хранятся,
но в задачи по выписке не идут, чтобы одна оплата не давала две задачи.
"""
import hashlib
import io
import re
from datetime import date, datetime

from sqlalchemy.orm import Session

from .. import models
from .qr_report import org_of

COLUMNS = ("Документ", "Дата операции", "Корреспондент", "Оборот Дт",
           "Оборот Кт", "Назначение платежа")


def _norm(text) -> str:
    return re.sub(r"\s+", " ", str(text or "").replace("\n", " ")).strip()


def _join(text) -> str:
    """Ячейка PDF, перенесённая по строкам: «2,187.00» и «7,181,019\\n.81»
    склеиваем без пробела, текст — через пробел."""
    return re.sub(r"\n(?=[.,\d])", "", str(text or ""))


def _money(text) -> float:
    s = _join(text).replace(",", "").replace(" ", "").strip()
    try:
        return float(s) if s else 0.0
    except ValueError:
        return 0.0


def _day(text) -> date | None:
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", str(text or ""))
    if not m:
        return None
    return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))


def _inn(text: str) -> str | None:
    m = re.search(r"ИНН\s*(\d{12,14})", text or "")
    return m.group(1) if m else None


def _party(text: str) -> str:
    """Корреспондент без ИНН, счёта и банка: «ИП Фамилия Имя, ИНН …,
    счёт № …» → «ИП Фамилия Имя»."""
    return _norm(text).split(", ИНН")[0].split(", счет")[0].strip()


def parse_pdf(content: bytes) -> dict:
    """Шапка и операции PDF-выписки. ValueError — файл не похож на выписку."""
    import pdfplumber

    head, rows = "", []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        if not pdf.pages:
            raise ValueError("Пустой PDF")
        head = pdf.pages[0].extract_text() or ""
        tail = pdf.pages[-1].extract_text() or ""
        for page in pdf.pages:
            for table in page.extract_tables():
                for raw in table:
                    cells = [c or "" for c in raw]
                    if len(cells) < 6 or _norm(cells[0]).startswith("Документ"):
                        continue
                    if _norm(cells[0]).upper() == "ИТОГО":
                        continue
                    day = _day(cells[1]) or _day(cells[0])
                    if day is None:
                        continue
                    rows.append({
                        "doc": _norm(cells[0]),
                        "date": day,
                        "party": _party(cells[2]),
                        "party_inn": _inn(_norm(cells[2])),
                        "out": _money(cells[3]),
                        "in": _money(cells[4]),
                        "purpose": _norm(_join(cells[5])),
                    })
    if "Выписка по счету" not in head:
        raise ValueError("Это не выписка банка: нет заголовка «Выписка по счету»")
    acc = re.search(r"Выписка по счету\s+(\d+)\s*([A-Z]{3})?", head)
    org_line = next((ln for ln in head.splitlines() if "ИНН" in ln and "Ос" in ln
                     or "ИНН" in ln and "ИП" in ln), "")
    period = re.search(r"с\s+(\d{2}\.\d{2}\.\d{4})\s+по\s+(\d{2}\.\d{2}\.\d{4})", head)
    opening = re.search(r"Входящий остаток:\s*([\d,.\s]+)", head)
    closing = re.search(r"Исходящий остаток:\s*([\d,.\s]+)", tail)
    bank = next((ln.split(" БИК")[0].strip() for ln in head.splitlines()
                 if "БИК" in ln), "")
    return {
        "account": acc.group(1) if acc else "",
        "currency": (acc.group(2) if acc and acc.group(2) else "KGS"),
        "bank": bank,
        "company": org_line.split(" ИНН")[0].strip(),
        "org": org_of(org_line),
        "date_from": _day(period.group(1)) if period else None,
        "date_to": _day(period.group(2)) if period else None,
        "opening": _money(opening.group(1)) if opening else None,
        "closing": _money(closing.group(1)) if closing else None,
        "rows": rows,
    }


def is_qr(purpose: str | None) -> bool:
    """QR своего банка («QR/…») и чужих («Покупка по QR (чужие)/…»)."""
    p = (purpose or "").upper()
    return p.startswith("QR/") or "ПО QR" in p


def is_fee(t) -> bool:
    """Комиссия банка за перевод. Базы проводят их то построчно, то сводно
    за день, — поэтому сверяются суммой за месяц, не построчно."""
    return "КОМИССИЯ" in (t.purpose or "").upper()


def _key(account: str, r: dict) -> str:
    """Строка выписки одна, сколько ни загружай пересекающиеся периоды:
    счёт, документ банка, дата и суммы."""
    raw = f"{account}|{r['doc']}|{r['date'].isoformat()}|{r['out']:.2f}|{r['in']:.2f}"
    return hashlib.sha256(raw.encode()).hexdigest()[:40]


def import_statement(db: Session, content: bytes, filename: str, user_id: int) -> dict:
    if not content.startswith(b"%PDF"):
        raise ValueError("Нужна PDF-выписка банка («Выписка по счету …»)")
    rep = parse_pdf(content)
    org = rep["org"]
    if org not in models.ORGS:
        raise ValueError(f"Не понять фирму по шапке выписки: «{rep['company']}»")
    rows = rep["rows"]
    if rep["opening"] is not None and rep["closing"] is not None and rows:
        # Остатки сходятся с оборотами — значит, все строки прочитаны.
        calc = rep["opening"] + sum(r["in"] for r in rows) - sum(r["out"] for r in rows)
        if abs(calc - rep["closing"]) > 0.05:
            raise ValueError(
                f"Выписка прочитана не полностью: по строкам остаток "
                f"{calc:,.2f}, а в выписке {rep['closing']:,.2f}")
    keys = [_key(rep["account"], r) for r in rows]
    known = {t.key for t in db.query(models.BankTxn.key).filter(
        models.BankTxn.key.in_(keys)).all()} if keys else set()
    added = 0
    for k, r in zip(keys, rows):
        if k in known:
            continue
        known.add(k)
        db.add(models.BankTxn(
            key=k, organization=org, account=rep["account"], bank=rep["bank"],
            currency=rep["currency"], doc=r["doc"], date=r["date"],
            amount_out=r["out"], amount_in=r["in"], party=r["party"],
            party_inn=r["party_inn"], purpose=r["purpose"],
            is_qr=is_qr(r["purpose"])))
        added += 1
    period = " – ".join(d.strftime("%d.%m.%Y") for d in (rep["date_from"], rep["date_to"]) if d)
    db.add(models.ImportLog(
        filename=f"[bank:{org}] {filename} · {rep['account']}" + (f" · {period}" if period else ""),
        user_id=user_id, added=added, skipped=len(rows) - added,
        errors_count=0, file_hash=hashlib.sha256(content).hexdigest()))
    db.commit()
    return {
        "organization": org, "account": rep["account"], "bank": rep["bank"],
        "date_from": rep["date_from"].isoformat() if rep["date_from"] else None,
        "date_to": rep["date_to"].isoformat() if rep["date_to"] else None,
        "total": len(rows), "added": added,
        "qr": sum(1 for r in rows if is_qr(r["purpose"])),
        "amount_in": round(sum(r["in"] for r in rows), 2),
        "amount_out": round(sum(r["out"] for r in rows), 2),
    }
