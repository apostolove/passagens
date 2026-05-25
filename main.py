"""
Mapa de Viagem — Proxy gratuito de dados de viagem
===================================================
Junta duas fontes 100% gratuitas e expõe endpoints simples e com CORS liberado
para o app (artifact) consumir do navegador, sem expor nenhum token:

  • Travelpayouts / Aviasales  -> preços REAIS de passagens (cache, afiliado grátis)
  • Open-Meteo                 -> clima REAL (previsão ou clima típico) sem chave

Variável de ambiente obrigatória:
  TRAVELPAYOUTS_TOKEN = seu token de afiliado (grátis)

Rodar local:
  pip install -r requirements.txt
  uvicorn main:app --reload
"""

import os
from datetime import date, datetime, timedelta

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

TP_TOKEN = os.environ.get("TRAVELPAYOUTS_TOKEN", "")
TP_BASE = "https://api.travelpayouts.com"

app = FastAPI(title="Mapa de Viagem — Proxy", version="1.0.0")

# Liberado para o app no navegador. São dados de leitura e o token fica no
# servidor; para uso pessoal está ok. Dá para restringir depois a claude.ai.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


async def _get(client: httpx.AsyncClient, url: str, params=None, headers=None):
    r = await client.get(url, params=params, headers=headers, timeout=25.0)
    r.raise_for_status()
    return r.json()


def _need_token():
    if not TP_TOKEN:
        raise HTTPException(
            status_code=500,
            detail="TRAVELPAYOUTS_TOKEN não configurado no servidor.",
        )


# ---------------------------------------------------------------------------
# Saúde
# ---------------------------------------------------------------------------
@app.get("/api/health")
async def health():
    return {"ok": True, "token_set": bool(TP_TOKEN)}


# ---------------------------------------------------------------------------
# Autocomplete cidade -> código IATA  (Travelpayouts, NÃO precisa de token)
# ---------------------------------------------------------------------------
@app.get("/api/airports")
async def airports(q: str = Query(..., min_length=2)):
    url = "https://autocomplete.travelpayouts.com/places2"
    params = {"term": q, "locale": "pt", "types[]": ["city", "airport"]}
    async with httpx.AsyncClient() as client:
        try:
            data = await _get(client, url, params=params)
        except httpx.HTTPError as e:
            raise HTTPException(502, f"Autocomplete falhou: {e}")
    out = []
    for p in data[:8]:
        out.append(
            {
                "code": p.get("code"),
                "name": p.get("name"),
                "country": p.get("country_name"),
                "type": p.get("type"),
            }
        )
    return {"ok": True, "results": out}


# ---------------------------------------------------------------------------
# DESCOBERTA: destinos mais baratos saindo de uma origem (city-directions)
# É o coração do "pra onde dá pra ir gastando pouco".
# ---------------------------------------------------------------------------
@app.get("/api/explore")
async def explore(
    origin: str = Query(..., description="IATA da origem, ex: SAO"),
    currency: str = "brl",
    depart_month: str | None = Query(None, description="YYYY-MM (opcional)"),
    direct: bool = False,
):
    _need_token()
    rows = []

    # 1) Tenta v3 prices_for_dates: traz duração, companhia, link e + destinos.
    v3 = f"{TP_BASE}/aviasales/v3/prices_for_dates"
    v3params = {
        "origin": origin.upper(),
        "currency": currency,
        "unique": "true",          # 1 (o mais barato) por destino
        "sorting": "price",
        "direct": str(direct).lower(),
        "limit": 1000,
        "token": TP_TOKEN,
    }
    if depart_month:
        v3params["departure_at"] = depart_month
    try:
        async with httpx.AsyncClient() as client:
            data = await _get(client, v3, params=v3params)
        for it in data.get("data") or []:
            rows.append(
                {
                    "destination": it.get("destination"),
                    "price": it.get("price"),
                    "currency": currency,
                    "departure_at": it.get("departure_at"),
                    "return_at": it.get("return_at"),
                    "transfers": it.get("transfers"),
                    "airline": it.get("airline"),
                    "duration_to": it.get("duration_to"),
                    "duration_back": it.get("duration_back"),
                    "link": it.get("link"),
                }
            )
    except httpx.HTTPError:
        rows = []

    # 2) Fallback: city-directions (sem duração) se o v3 não trouxe nada.
    if not rows:
        try:
            async with httpx.AsyncClient() as client:
                data = await _get(
                    client,
                    f"{TP_BASE}/v1/city-directions",
                    params={"origin": origin.upper(), "currency": currency},
                    headers={"X-Access-Token": TP_TOKEN},
                )
            for dest, info in (data.get("data") or {}).items():
                rows.append(
                    {
                        "destination": dest,
                        "price": info.get("price"),
                        "currency": currency,
                        "departure_at": info.get("departure_at"),
                        "return_at": info.get("return_at"),
                        "transfers": info.get("transfers"),
                        "airline": info.get("airline"),
                        "duration_to": None,
                        "duration_back": None,
                        "link": None,
                    }
                )
        except httpx.HTTPError as e:
            raise HTTPException(502, f"Travelpayouts falhou: {e}")

    rows.sort(key=lambda r: (r["price"] is None, r["price"] or 0))
    return {"ok": True, "origin": origin.upper(), "results": rows}


# ---------------------------------------------------------------------------
# CALENDÁRIO de preços: voo mais barato por dia do mês (escolher a melhor data)
# ---------------------------------------------------------------------------
@app.get("/api/calendar")
async def calendar(
    origin: str,
    destination: str,
    depart_month: str = Query(..., description="YYYY-MM"),
    return_month: str | None = None,
    currency: str = "brl",
    direct: bool = False,
):
    _need_token()
    days_map = {}

    # 1) v3 prices_for_dates: ida-volta, MESMA fonte dos cards -> números batem.
    v3 = f"{TP_BASE}/aviasales/v3/prices_for_dates"
    v3params = {
        "origin": origin.upper(),
        "destination": destination.upper(),
        "departure_at": depart_month,
        "currency": currency,
        "unique": "false",
        "sorting": "price",
        "direct": str(direct).lower(),
        "limit": 1000,
        "token": TP_TOKEN,
    }
    try:
        async with httpx.AsyncClient() as client:
            data = await _get(client, v3, params=v3params)
        for it in data.get("data") or []:
            dep = (it.get("departure_at") or "")[:10]  # YYYY-MM-DD
            price = it.get("price")
            if not dep.startswith(depart_month) or price is None:
                continue  # trava no mês pedido (sem vazar p/ mês vizinho)
            cur = days_map.get(dep)
            if cur is None or price < cur["price"]:
                days_map[dep] = {
                    "date": dep,
                    "price": price,
                    "transfers": it.get("transfers"),
                    "airline": it.get("airline"),
                    "departure_at": it.get("departure_at"),
                    "return_at": it.get("return_at"),
                }
    except httpx.HTTPError:
        days_map = {}

    # 2) Fallback: v1 prices/calendar se o v3 não trouxe nada.
    if not days_map:
        url = f"{TP_BASE}/v1/prices/calendar"
        params = {
            "origin": origin.upper(),
            "destination": destination.upper(),
            "depart_date": depart_month,
            "calendar_type": "departure_date",
            "currency": currency,
        }
        if return_month:
            params["return_date"] = return_month
        try:
            async with httpx.AsyncClient() as client:
                data = await _get(client, url, params=params, headers={"X-Access-Token": TP_TOKEN})
            for day, info in (data.get("data") or {}).items():
                d = day[:10]
                if not d.startswith(depart_month):
                    continue
                days_map[d] = {
                    "date": d,
                    "price": info.get("price"),
                    "transfers": info.get("transfers"),
                    "airline": info.get("airline"),
                    "departure_at": info.get("departure_at"),
                    "return_at": info.get("return_at"),
                }
        except httpx.HTTPError as e:
            raise HTTPException(502, f"Travelpayouts falhou: {e}")

    days = sorted(days_map.values(), key=lambda d: d["date"])
    cheapest = min((d for d in days if d["price"] is not None), key=lambda d: d["price"], default=None)
    return {"ok": True, "currency": currency, "cheapest": cheapest, "days": days}


# ---------------------------------------------------------------------------
# ROTA específica (v3): tarifas mais baratas para datas dadas
# ---------------------------------------------------------------------------
@app.get("/api/route")
async def route(
    origin: str,
    destination: str,
    departure_at: str = Query(..., description="YYYY-MM ou YYYY-MM-DD"),
    return_at: str | None = None,
    currency: str = "brl",
    one_way: bool = False,
    limit: int = 30,
):
    _need_token()
    url = f"{TP_BASE}/aviasales/v3/prices_for_dates"
    params = {
        "origin": origin.upper(),
        "destination": destination.upper(),
        "departure_at": departure_at,
        "currency": currency,
        "one_way": str(one_way).lower(),
        "sorting": "price",
        "direct": "false",
        "limit": limit,
        "token": TP_TOKEN,
    }
    if return_at:
        params["return_at"] = return_at
    async with httpx.AsyncClient() as client:
        try:
            data = await _get(client, url, params=params)
        except httpx.HTTPError as e:
            raise HTTPException(502, f"Travelpayouts falhou: {e}")
    return {"ok": True, "currency": currency, "results": data.get("data", [])}


# ---------------------------------------------------------------------------
# CLIMA real (Open-Meteo, sem chave). Previsão se a data está perto,
# senão o clima TÍPICO do mesmo período no ano anterior.
# ---------------------------------------------------------------------------
async def _geocode(client, city):
    data = await _get(
        client,
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": city, "count": 1, "language": "pt"},
    )
    res = (data or {}).get("results") or []
    if not res:
        return None
    return res[0]["latitude"], res[0]["longitude"], res[0].get("name")


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 1) if xs else None


@app.get("/api/weather")
async def weather(city: str, start: str, end: str):
    """start/end no formato YYYY-MM-DD."""
    try:
        s = datetime.strptime(start, "%Y-%m-%d").date()
        e = datetime.strptime(end, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(400, "Datas devem ser YYYY-MM-DD")

    async with httpx.AsyncClient() as client:
        geo = await _geocode(client, city)
        if not geo:
            raise HTTPException(404, f"Cidade não encontrada: {city}")
        lat, lon, name = geo

        near = (s - date.today()).days <= 16
        if near:
            url = "https://api.open-meteo.com/v1/forecast"
            params = {
                "latitude": lat, "longitude": lon,
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_mean",
                "start_date": start, "end_date": end, "timezone": "auto",
            }
            kind = "previsao"
        else:
            url = "https://archive-api.open-meteo.com/v1/archive"
            params = {
                "latitude": lat, "longitude": lon,
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
                "start_date": f"{s.year - 1}-{s.month:02d}-{s.day:02d}",
                "end_date": f"{e.year - 1}-{e.month:02d}-{e.day:02d}",
                "timezone": "auto",
            }
            kind = "tipico"
        try:
            data = await _get(client, url, params=params)
        except httpx.HTTPError as ex:
            raise HTTPException(502, f"Open-Meteo falhou: {ex}")

    daily = data.get("daily", {})
    return {
        "ok": True,
        "city": name,
        "kind": kind,
        "temp_max_avg": _avg(daily.get("temperature_2m_max", [])),
        "temp_min_avg": _avg(daily.get("temperature_2m_min", [])),
        "rain_signal": _avg(
            daily.get("precipitation_probability_mean")
            or daily.get("precipitation_sum", [])
        ),
        "rain_field": "prob_chuva_%" if near else "chuva_mm_total_dia",
    }
