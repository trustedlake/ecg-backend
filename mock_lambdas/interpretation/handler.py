"""Lambda MOCK de interpretação de ECG (contrato v1, operation=interpret).

Valida sinal e sexo do paciente de verdade. A frequência cardíaca e as
amplitudes saem do sinal recebido (detecção simples de picos R); os demais
intervalos são fixos. Os limiares dependentes do sexo (QTc, Cornell) são
aplicados conforme o paciente. Não substitui laudo médico. Sem dependências
além da stdlib. Ver CONTRATO_LAMBDAS.md.
"""

import json
import logging
import math
import os

from ecg_contract import (
    INTERP_FAILURE_CODES, SCENARIO_FAILURE, SCENARIO_SUCCESS, SCENARIO_WARNINGS,
    STANDARD_LEADS, ContractError, Envelope, resolve_mock, validate_common, warning,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ENGINE = "ecg-interpretation-mock"
ENGINE_VERSION = os.environ.get("CODE_VERSION", "dev")
SEXES = ("F", "M")
MIN_RATE_HZ, MAX_RATE_HZ = 100, 2000
MAX_SECONDS_PER_LEAD = 60
SCENARIOS = (SCENARIO_SUCCESS, SCENARIO_WARNINGS, SCENARIO_FAILURE)
SCENARIO_CODES = {SCENARIO_FAILURE: INTERP_FAILURE_CODES}

# Limiares por sexo (critérios clássicos; valores de referência do mock).
QTC_LIMIT_MS = {"F": 460, "M": 450}
CORNELL_LIMIT_MV = {"F": 2.0, "M": 2.8}
SOKOLOW_LIMIT_MV = 3.5
FIXED_INTERVALS_MS = {"pr_ms": 160, "qrs_ms": 92, "qt_ms": 380}
DISCLAIMER = ("Resultado gerado automaticamente a partir do sinal extraído. "
              "Não substitui a avaliação e o laudo de um médico.")


def handler(event, context):
    env = Envelope("interpret", ENGINE, ENGINE_VERSION)
    try:
        env.request_id = validate_common(event)
        scenario, code = resolve_mock(event, os.environ.get("MOCK_SCENARIO"), SCENARIOS, SCENARIO_CODES)
        env.scenario = scenario
        sex = _validate_patient(event.get("patient"))
        rate, leads = _validate_signal(event.get("signal"))
        if scenario == SCENARIO_FAILURE:
            raise ContractError(code or "INTERP_NO_QRS_DETECTED")
        result, warnings = _interpret(sex, rate, leads, degraded=scenario == SCENARIO_WARNINGS)
        response = env.success(result, warnings)
    except ContractError as exc:
        logger.info("interpret %s: %s", env.request_id, exc)
        response = env.failure(exc)
    except Exception:  # nunca vazar FunctionError para o backend
        logger.exception("interpret %s: erro inesperado", env.request_id)
        response = env.failure(ContractError("INTERNAL_ERROR"))
    logger.info(json.dumps({"request_id": response["request_id"], "status": response["status"],
                            "error": (response["error"] or {}).get("code"),
                            "scenario": env.scenario, "duration_ms": response["meta"]["duration_ms"]}))
    return response


# ─── Validação ────────────────────────────────────────────────────────────────

def _validate_patient(patient):
    sex = patient.get("sex") if isinstance(patient, dict) else None
    if sex in (None, ""):
        raise ContractError("SEX_MISSING")
    if sex not in SEXES:
        raise ContractError("SEX_INVALID", details={"received": sex, "allowed": list(SEXES)})
    return sex


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _validate_signal(signal):
    if signal is None:
        raise ContractError("SIGNAL_MISSING")
    if not isinstance(signal, dict):
        raise ContractError("SIGNAL_INVALID", details={"reason": "signal deve ser um objeto"})
    rate = signal.get("sampling_rate_hz")
    if not _is_number(rate) or not MIN_RATE_HZ <= rate <= MAX_RATE_HZ:
        raise ContractError("SIGNAL_SAMPLING_RATE_INVALID", details={"received": rate})
    if signal.get("units", "mV") != "mV":
        raise ContractError("SIGNAL_INVALID", details={"reason": "units deve ser \"mV\""})
    leads = signal.get("leads")
    if not isinstance(leads, list) or not leads:
        raise ContractError("SIGNAL_MISSING")
    for index, lead in enumerate(leads):
        if not isinstance(lead, dict):
            raise ContractError("SIGNAL_INVALID", details={"lead_index": index, "reason": "derivação deve ser objeto"})
        name, samples = lead.get("name"), lead.get("samples")
        if name not in STANDARD_LEADS:
            raise ContractError("SIGNAL_LEAD_UNKNOWN", details={"lead_index": index, "received": name,
                                                                "allowed": list(STANDARD_LEADS)})
        if not isinstance(samples, list) or not all(v is None or _is_number(v) for v in samples):
            raise ContractError("SIGNAL_INVALID", details={"lead": name, "reason": "samples deve ser lista de números ou null"})
        if len(samples) > MAX_SECONDS_PER_LEAD * rate:
            raise ContractError("SIGNAL_INVALID", details={"lead": name, "reason": f"máximo {MAX_SECONDS_PER_LEAD} s por derivação"})
        if sum(v is not None for v in samples) < rate:
            raise ContractError("SIGNAL_TOO_SHORT", details={"lead": name, "valid_samples": sum(v is not None for v in samples)})
    return float(rate), leads


# ─── Interpretação (mock) ─────────────────────────────────────────────────────

def _r_peaks(samples, rate):
    values = [v for v in samples if v is not None]
    top = max(abs(v) for v in values) if values else 0
    if top <= 0:
        return []
    sign = 1 if max(values) >= -min(values) else -1
    threshold, refractory = 0.6 * top, int(0.25 * rate)
    peaks, last = [], -refractory
    for i in range(1, len(samples) - 1):
        cur, prev, nxt = samples[i], samples[i - 1], samples[i + 1]
        if cur is None or prev is None or nxt is None:
            continue
        if sign * cur >= threshold and sign * cur >= sign * prev and sign * cur > sign * nxt and i - last >= refractory:
            peaks.append(i)
            last = i
    return peaks


def _heart_rate(leads, rate):
    # A derivação mais longa (normalmente a tira de ritmo) dá a melhor estimativa.
    for lead in sorted(leads, key=lambda lead: (-len(lead["samples"]), lead["name"] != "II")):
        peaks = _r_peaks(lead["samples"], rate)
        if len(peaks) >= 2:
            rr = (peaks[-1] - peaks[0]) / (len(peaks) - 1) / rate
            return round(60.0 / rr), rr, lead["name"]
    raise ContractError("INTERP_NO_QRS_DETECTED")


def _amplitudes(leads):
    per_lead = {}
    for lead in leads:
        values = [v for v in lead["samples"] if v is not None]
        current = per_lead.setdefault(lead["name"], {"name": lead["name"], "r_amplitude_mv": 0.0,
                                                     "s_amplitude_mv": 0.0, "quality": "good"})
        current["r_amplitude_mv"] = round(max(current["r_amplitude_mv"], max(values)), 3)
        current["s_amplitude_mv"] = round(max(current["s_amplitude_mv"], -min(values)), 3)
    return per_lead


def _criterion(name, value, unit, limit, op, sex_specific, sex, available=True):
    if not available:
        return {"name": name, "value": None, "unit": unit, "reference": f"{op} {limit} {unit}",
                "sex_specific": sex_specific, "status": "not_evaluated"}
    abnormal = value > limit
    reference = f"≤ {limit} {unit}" + (f" ({'feminino' if sex == 'F' else 'masculino'})" if sex_specific else "")
    return {"name": name, "value": value, "unit": unit, "reference": reference,
            "sex_specific": sex_specific, "status": "abnormal" if abnormal else "normal"}


def _interpret(sex, rate, leads, degraded):
    warnings = [warning("LEAD_MISSING", lead=name) for name in STANDARD_LEADS
                if name not in {lead["name"] for lead in leads}]
    hr, rr, hr_lead = _heart_rate(leads, rate)
    per_lead = _amplitudes(leads)
    qt = FIXED_INTERVALS_MS["qt_ms"]
    qtc = round(qt / math.sqrt(rr))
    confidence = {"heart_rate_bpm": "high", "pr_ms": "medium", "qrs_ms": "medium", "qt_ms": "medium", "qtc_ms": "medium"}
    if degraded:
        low = "V2" if "V2" in per_lead else next(iter(per_lead))
        per_lead[low]["quality"] = "low"
        warnings.append(warning("LEAD_LOW_QUALITY", lead=low))
        warnings.append(warning("MEASUREMENT_UNRELIABLE", message="Intervalo QT medido com baixa confiança (onda T achatada)."))
        confidence["qt_ms"] = confidence["qtc_ms"] = "low"

    def amp(name, field):
        return per_lead[name][field] if name in per_lead else None

    cornell_ok = amp("aVL", "r_amplitude_mv") is not None and amp("V3", "s_amplitude_mv") is not None
    cornell = round(amp("aVL", "r_amplitude_mv") + amp("V3", "s_amplitude_mv"), 3) if cornell_ok else None
    sokolow_ok = amp("V1", "s_amplitude_mv") is not None and (amp("V5", "r_amplitude_mv") is not None or amp("V6", "r_amplitude_mv") is not None)
    sokolow = round(amp("V1", "s_amplitude_mv") + max(amp("V5", "r_amplitude_mv") or 0, amp("V6", "r_amplitude_mv") or 0), 3) if sokolow_ok else None
    criteria = [
        _criterion("QTc (Bazett)", qtc, "ms", QTC_LIMIT_MS[sex], "≤", True, sex),
        _criterion("Cornell (R aVL + S V3)", cornell, "mV", CORNELL_LIMIT_MV[sex], "≤", True, sex, cornell_ok),
        _criterion("Sokolow-Lyon (S V1 + R V5/V6)", sokolow, "mV", SOKOLOW_LIMIT_MV, "≤", False, sex, sokolow_ok),
    ]

    findings = []
    if hr < 60:
        findings.append(("SINUS_BRADYCARDIA", "Bradicardia sinusal", "warning", f"FC {hr} bpm (< 60)"))
    elif hr > 100:
        findings.append(("SINUS_TACHYCARDIA", "Taquicardia sinusal", "warning", f"FC {hr} bpm (> 100)"))
    else:
        findings.append(("SINUS_RHYTHM", "Ritmo sinusal", "info", f"FC {hr} bpm, RR regular"))
    net_i = (amp("I", "r_amplitude_mv") or 0) - (amp("I", "s_amplitude_mv") or 0)
    net_avf = (amp("aVF", "r_amplitude_mv") or 0) - (amp("aVF", "s_amplitude_mv") or 0)
    if "I" in per_lead and "aVF" in per_lead:
        if net_i > 0 and net_avf > 0:
            findings.append(("NORMAL_AXIS", "Eixo elétrico normal", "info", "QRS predominantemente positivo em I e aVF"))
        else:
            findings.append(("AXIS_DEVIATION", "Desvio do eixo elétrico", "warning",
                             f"QRS líquido I {net_i:+.2f} mV, aVF {net_avf:+.2f} mV"))
    if criteria[0]["status"] == "abnormal":
        findings.append(("QTC_PROLONGED", "QTc prolongado", "warning", f"QTc {qtc} ms ({criteria[0]['reference']})"))
    if any(c["status"] == "abnormal" for c in criteria[1:]):
        findings.append(("LVH_VOLTAGE", "Critério de voltagem para sobrecarga ventricular esquerda", "warning",
                         "Cornell e/ou Sokolow-Lyon acima do limite"))
    findings = [{"code": c, "label": label, "severity": sev, "evidence": ev} for c, label, sev, ev in findings]

    abnormal = [f for f in findings if f["severity"] != "info"]
    classification = "abnormal" if abnormal else ("borderline" if degraded else "normal")
    headline = {"normal": "ECG dentro dos limites da normalidade",
                "borderline": "ECG sem alterações relevantes, com medidas de baixa confiança",
                "abnormal": "ECG com alterações; revisar achados"}[classification]
    rhythm = findings[0]["label"]
    summary = {
        "classification": classification,
        "headline": headline,
        "rhythm": rhythm,
        "heart_rate_bpm": hr,
        "key_findings": [f["label"] for f in findings],
        "text": f"{rhythm}, FC {hr} bpm, QTc {qtc} ms. {headline}.",
    }
    details = {
        "measurements": {
            "heart_rate_bpm": {"value": hr, "unit": "bpm", "confidence": confidence["heart_rate_bpm"], "lead": hr_lead},
            "rr_ms": {"value": round(rr * 1000), "unit": "ms", "confidence": confidence["heart_rate_bpm"]},
            "pr_ms": {"value": FIXED_INTERVALS_MS["pr_ms"], "unit": "ms", "confidence": confidence["pr_ms"]},
            "qrs_ms": {"value": FIXED_INTERVALS_MS["qrs_ms"], "unit": "ms", "confidence": confidence["qrs_ms"]},
            "qt_ms": {"value": qt, "unit": "ms", "confidence": confidence["qt_ms"]},
            "qtc_ms": {"value": qtc, "unit": "ms", "confidence": confidence["qtc_ms"], "formula": "Bazett"},
            "qrs_axis_deg": {"value": 45, "unit": "deg", "confidence": "medium"},
        },
        "findings": findings,
        "criteria": criteria,
        "per_lead": [per_lead[name] for name in STANDARD_LEADS if name in per_lead],
    }
    return {"patient": {"sex": sex}, "summary": summary, "details": details, "disclaimer": DISCLAIMER}, warnings
