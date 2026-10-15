"""Contrato v1 das lambdas de leitura e interpretação de ECG.

Módulo compartilhado pelas lambdas mock (e reutilizável pelas reais):
catálogo de erros/avisos, layouts suportados e o envelope de resposta.
A especificação legível fica em CONTRATO_LAMBDAS.md, na raiz do repo.
"""

import time
import uuid

CONTRACT_VERSION = "1.0"

STATUS_OK = "OK"
STATUS_OK_WITH_WARNINGS = "OK_WITH_WARNINGS"
STATUS_REFUSED = "REFUSED"
STATUS_ERROR = "ERROR"

# Ordem padrão das 12 derivações.
STANDARD_LEADS = ("I", "II", "III", "aVR", "aVL", "aVF", "V1", "V2", "V3", "V4", "V5", "V6")

# layout -> (grade de derivações por linha, derivações de ritmo de 10 s).
# Cada linha da grade ocupa 10 s de papel, dividida igualmente entre as colunas.
LAYOUTS = {
    "3x4": ((("I", "aVR", "V1", "V4"), ("II", "aVL", "V2", "V5"), ("III", "aVF", "V3", "V6")), ()),
    "3x4+1": ((("I", "aVR", "V1", "V4"), ("II", "aVL", "V2", "V5"), ("III", "aVF", "V3", "V6")), ("II",)),
    "3x4+3": ((("I", "aVR", "V1", "V4"), ("II", "aVL", "V2", "V5"), ("III", "aVF", "V3", "V6")), ("V1", "II", "V5")),
    "6x2": (tuple(zip(("I", "II", "III", "aVR", "aVL", "aVF"), ("V1", "V2", "V3", "V4", "V5", "V6"))), ()),
    "6x2+1": (tuple(zip(("I", "II", "III", "aVR", "aVL", "aVF"), ("V1", "V2", "V3", "V4", "V5", "V6"))), ("II",)),
    "12x1": (tuple((lead,) for lead in STANDARD_LEADS), ()),
}

PAPER_SECONDS = 10.0
SPEEDS_MM_S = (12.5, 25.0, 50.0)
GAINS_MM_MV = (5.0, 10.0, 20.0)

SCENARIO_SUCCESS = "success"
SCENARIO_WARNINGS = "success_with_warnings"
SCENARIO_FAILURE = "failure"
SCENARIO_REFUSED = "refused"

# código -> (categoria, retryable, mensagem padrão em pt-BR)
ERRORS = {
    # Comuns
    "INVALID_PAYLOAD": ("validation", False, "O payload precisa ser um objeto JSON."),
    "UNSUPPORTED_CONTRACT_VERSION": ("validation", False, "Versão de contrato não suportada; use 1.0."),
    "REQUEST_ID_INVALID": ("validation", False, "request_id ausente ou inválido (use o UUID da tentativa)."),
    "MOCK_SCENARIO_INVALID": ("validation", False, "Cenário de teste (mock) inválido."),
    "STORAGE_ERROR": ("internal", True, "Falha ao ler ou gravar artefatos no S3."),
    "INTERNAL_ERROR": ("internal", True, "Erro interno inesperado na lambda."),
    # Leitura — validação de entrada
    "IMAGE_MISSING": ("validation", False, "Informe a imagem do ECG (image.bucket e image.key)."),
    "IMAGE_NOT_FOUND": ("validation", False, "A imagem informada não existe no S3."),
    "IMAGE_FORMAT_UNSUPPORTED": ("validation", False, "Formato de imagem não suportado; envie JPG, PNG ou WebP."),
    "IMAGE_TOO_LARGE": ("validation", False, "A imagem excede o tamanho máximo de 15 MB."),
    "IMAGE_TOO_SMALL": ("validation", False, "Resolução insuficiente; a imagem deve ter ao menos 800x500 px."),
    "IMAGE_UNREADABLE": ("validation", False, "Não foi possível decodificar a imagem enviada."),
    "LAYOUT_MISSING": ("validation", False, "Informe o layout de derivações (ex.: 3x4+1)."),
    "LAYOUT_UNSUPPORTED": ("validation", False, "Layout de derivações não suportado."),
    "CALIBRATION_INVALID": ("validation", False, "Calibração inválida; velocidade 12.5/25/50 mm/s e ganho 5/10/20 mm/mV."),
    # Leitura — recusa (empecilho visível; o usuário deve fotografar de novo)
    "READ_REFUSED_BLURRY": ("refused", False, "A imagem está borrada ou fora de foco; fotografe novamente com o papel nítido."),
    "READ_REFUSED_CROPPED": ("refused", False, "O traçado está cortado nas bordas; enquadre o papel inteiro na foto."),
    "READ_REFUSED_OCCLUDED": ("refused", False, "Há sobreposição sobre o traçado (dedo, reflexo, objeto ou anotação); remova e fotografe novamente."),
    "READ_REFUSED_LOW_CONTRAST": ("refused", False, "Contraste insuficiente entre o traçado e a grade; melhore a iluminação."),
    "READ_REFUSED_NOT_ECG": ("refused", False, "A imagem não parece ser um ECG em papel."),
    "READ_REFUSED_LAYOUT_MISMATCH": ("refused", False, "O layout detectado não corresponde ao layout informado."),
    # Leitura — falha de processamento
    "READ_GRID_NOT_FOUND": ("processing", False, "Não foi possível detectar a grade milimetrada do papel."),
    "READ_TRACE_EXTRACTION_FAILED": ("processing", False, "Não foi possível extrair o traçado das derivações."),
    # Interpretação — validação de entrada
    "SEX_MISSING": ("validation", False, "Informe o sexo do paciente (patient.sex)."),
    "SEX_INVALID": ("validation", False, "Sexo do paciente inválido; use \"F\" ou \"M\"."),
    "SIGNAL_MISSING": ("validation", False, "Informe o sinal do ECG (signal)."),
    "SIGNAL_INVALID": ("validation", False, "Sinal do ECG em formato inválido."),
    "SIGNAL_SAMPLING_RATE_INVALID": ("validation", False, "Taxa de amostragem inválida; use entre 100 e 2000 Hz."),
    "SIGNAL_LEAD_UNKNOWN": ("validation", False, "Derivação desconhecida no sinal."),
    "SIGNAL_TOO_SHORT": ("validation", False, "Sinal curto demais; cada derivação precisa de ao menos 1 s."),
    # Interpretação — falha de processamento
    "INTERP_NO_QRS_DETECTED": ("processing", False, "Nenhum complexo QRS detectado no sinal."),
    "INTERP_SIGNAL_TOO_NOISY": ("processing", False, "Sinal ruidoso demais para interpretação confiável."),
}

WARNINGS = {
    # Leitura
    "LEAD_PARTIAL_TRACE": "Traçado extraído parcialmente; trechos sem leitura vêm como null.",
    "LEAD_SATURATED": "Traçado encosta na derivação vizinha; amplitudes podem estar cortadas.",
    "IMAGE_ROTATION_CORRECTED": "A imagem estava inclinada e foi alinhada automaticamente.",
    "CALIBRATION_PULSE_NOT_FOUND": "Pulso de calibração não encontrado; usada a calibração informada.",
    # Interpretação
    "LEAD_MISSING": "Derivação ausente no sinal; critérios que dependem dela foram omitidos.",
    "LEAD_LOW_QUALITY": "Derivação com qualidade baixa; medidas nela são menos confiáveis.",
    "MEASUREMENT_UNRELIABLE": "Medida calculada com baixa confiança.",
}

READ_FAILURE_CODES = ("READ_GRID_NOT_FOUND", "READ_TRACE_EXTRACTION_FAILED", "STORAGE_ERROR", "INTERNAL_ERROR")
READ_REFUSAL_CODES = tuple(code for code, (category, _, _) in ERRORS.items() if category == "refused")
INTERP_FAILURE_CODES = ("INTERP_NO_QRS_DETECTED", "INTERP_SIGNAL_TOO_NOISY", "INTERNAL_ERROR")


class ContractError(Exception):
    """Erro do catálogo: vira um envelope ERROR ou REFUSED, nunca FunctionError."""

    def __init__(self, code, message=None, details=None):
        if code not in ERRORS:
            raise ValueError(f"código de erro fora do catálogo: {code}")
        self.code = code
        self.message = message or ERRORS[code][2]
        self.details = details or {}
        super().__init__(f"{code}: {self.message}")

    def as_dict(self):
        category, retryable, _ = ERRORS[self.code]
        return {"code": self.code, "category": category, "retryable": retryable,
                "message": self.message, "details": self.details}


def warning(code, lead=None, message=None):
    if code not in WARNINGS:
        raise ValueError(f"código de aviso fora do catálogo: {code}")
    item = {"code": code, "message": message or WARNINGS[code]}
    if lead:
        item["lead"] = lead
    return item


class Envelope:
    """Monta a resposta padrão; mede a duração desde a criação."""

    def __init__(self, operation, engine, engine_version):
        self.operation = operation
        self.engine = engine
        self.engine_version = engine_version
        self.request_id = None
        self.scenario = None
        self._started = time.monotonic()

    def _build(self, status, result=None, warnings=None, error=None):
        meta = {"engine": self.engine, "engine_version": self.engine_version,
                "duration_ms": int((time.monotonic() - self._started) * 1000)}
        if self.scenario:
            meta["mock_scenario"] = self.scenario
        return {"contract_version": CONTRACT_VERSION, "operation": self.operation,
                "request_id": self.request_id, "status": status,
                "warnings": warnings or [], "error": error, "result": result, "meta": meta}

    def success(self, result, warnings=None):
        status = STATUS_OK_WITH_WARNINGS if warnings else STATUS_OK
        return self._build(status, result=result, warnings=warnings)

    def failure(self, exc):
        status = STATUS_REFUSED if ERRORS[exc.code][0] == "refused" else STATUS_ERROR
        return self._build(status, error=exc.as_dict())


def validate_common(event):
    """Valida o que é comum às duas lambdas e devolve (request_id, mock)."""
    if not isinstance(event, dict):
        raise ContractError("INVALID_PAYLOAD")
    version = event.get("contract_version", CONTRACT_VERSION)
    if version != CONTRACT_VERSION:
        raise ContractError("UNSUPPORTED_CONTRACT_VERSION", details={"received": version})
    request_id = event.get("request_id")
    try:
        request_id = str(uuid.UUID(str(request_id)))
    except (ValueError, TypeError):
        raise ContractError("REQUEST_ID_INVALID", details={"received": request_id}) from None
    return request_id


def resolve_mock(event, default_scenario, allowed_scenarios, allowed_codes):
    """Escolhe o cenário mock: payload `mock` > env MOCK_SCENARIO > success.

    Exclusivo das lambdas mock; as reais devem ignorar o campo `mock`.
    """
    mock = event.get("mock") if isinstance(event, dict) else None
    if mock is None:
        mock = {}
    if not isinstance(mock, dict):
        raise ContractError("MOCK_SCENARIO_INVALID", details={"received": mock})
    scenario = mock.get("scenario") or default_scenario or SCENARIO_SUCCESS
    if scenario not in allowed_scenarios:
        raise ContractError("MOCK_SCENARIO_INVALID",
                            details={"received": scenario, "allowed": list(allowed_scenarios)})
    code = mock.get("code")
    if code is not None:
        allowed = allowed_codes.get(scenario, ())
        if code not in allowed:
            raise ContractError("MOCK_SCENARIO_INVALID",
                                details={"received_code": code, "allowed_codes": list(allowed)})
    return scenario, code
