"""
Monitor de anúncios do Facebook Marketplace com aviso no WhatsApp (CallMeBot)
e/ou Telegram. Feito para rodar de graça no GitHub Actions ou num celular
Android (Termux).

Uso:
  python monitor.py              -> roda uma vez (modo normal, usado no GitHub Actions)
  python monitor.py --teste      -> só mostra o que encontrou; não salva nem notifica
  python monitor.py --loop 5     -> roda para sempre, a cada 5 minutos (Termux/PC)
  python monitor.py --testar-whatsapp -> manda uma mensagem de teste

Variáveis de ambiente (no GitHub ficam em Settings > Secrets):
  CALLMEBOT_PHONE   número com DDI, ex: +5569999999999
  CALLMEBOT_APIKEY  chave recebida do CallMeBot
  GREENAPI_ID       idInstance da Green-API (alternativa ao CallMeBot)
  GREENAPI_TOKEN    apiTokenInstance da Green-API
  GREENAPI_DESTINO  número que recebe, com DDI, ex: 5569999999999
  GREENAPI_URL      (opcional) apiUrl mostrada no console, ex: https://7103.api.greenapi.com
  TELEGRAM_TOKEN    (opcional) token do bot do Telegram
  TELEGRAM_CHAT_ID  (opcional) chat id(s) fixos, separados por vírgula.
                    Além deles, quem mandar /start no bot passa a receber (/parar sai).
  FB_COOKIES        (opcional) cookies de uma conta secundária, ex: "c_user=...; xs=...; datr=..."
"""

import argparse
import json
import os
import random
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests

PASTA = Path(__file__).resolve().parent
ARQ_CONFIG = PASTA / "config.json"
ARQ_ESTADO = PASTA / "estado.json"

MAX_VISTOS = 5000                 # quantos IDs guardar no histórico
FALHAS_PARA_AVISAR = 6            # execuções seguidas sem resultado antes de avisar
MAX_NOTIFICACOES_POR_RODADA = 15  # trava contra enxurrada de mensagens

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #
def normalizar(texto: str) -> str:
    """minúsculas, sem acento, espaços simples."""
    texto = unicodedata.normalize("NFKD", texto or "")
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", texto.lower()).strip()


def log(msg: str) -> None:
    agora = datetime.now().strftime("%H:%M:%S")
    print(f"[{agora}] {msg}", flush=True)


def carregar_json(caminho: Path, padrao):
    if caminho.exists():
        with open(caminho, encoding="utf-8") as f:
            return json.load(f)
    return padrao


def salvar_estado(estado: dict) -> None:
    # mantém só os IDs mais recentes
    vistos = estado["vistos"]
    if len(vistos) > MAX_VISTOS:
        ordenados = sorted(vistos.items(), key=lambda kv: kv[1], reverse=True)
        estado["vistos"] = dict(ordenados[:MAX_VISTOS])
    with open(ARQ_ESTADO, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=1, sort_keys=True)


def estado_inicial() -> dict:
    return {
        "vistos": {},
        "buscas_inicializadas": [],
        "falhas_seguidas": 0,
        "aviso_bloqueio_enviado": False,
        "telegram_assinantes": [],
        "telegram_offset": 0,
    }


# --------------------------------------------------------------------------- #
# Coleta no Facebook
# --------------------------------------------------------------------------- #
def montar_url(local: str, termo: str, raio_km: int) -> str:
    return (
        f"https://www.facebook.com/marketplace/{local}/search/"
        f"?query={quote(termo)}&sortBy=creation_time_descend"
        f"&radius={raio_km}&exact=false"
    )


def _percorrer(obj, achados: dict) -> None:
    """Procura, em qualquer profundidade, objetos que pareçam um anúncio."""
    if isinstance(obj, dict):
        if "marketplace_listing_title" in obj and obj.get("id"):
            achados[str(obj["id"])] = obj
        for v in obj.values():
            _percorrer(v, achados)
    elif isinstance(obj, list):
        for v in obj:
            _percorrer(v, achados)


CAMPOS_DATA = ("creation_time", "created_time", "listing_creation_time", "publish_time")


def _achar_data(obj, profundidade: int = 0):
    """Procura um timestamp de criação (segundos unix) dentro de um nó."""
    if profundidade > 4:
        return None
    if isinstance(obj, dict):
        for k in CAMPOS_DATA:
            v = obj.get(k)
            if isinstance(v, (int, float)) and 1_000_000_000 < v < 4_000_000_000:
                return int(v)
        for v in obj.values():
            r = _achar_data(v, profundidade + 1)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj[:20]:
            r = _achar_data(v, profundidade + 1)
            if r:
                return r
    return None


def data_do_anuncio(sessao: requests.Session, anuncio: dict):
    """Abre a página do anúncio e devolve o timestamp de publicação (ou None)."""
    try:
        r = sessao.get(anuncio["link"], headers=HEADERS, timeout=30)
    except requests.RequestException:
        return None
    alvo = {}
    for bloco in re.findall(r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', r.text, re.S):
        if anuncio["id"] not in bloco or "creation_time" not in bloco:
            continue
        try:
            dados = json.loads(bloco)
        except json.JSONDecodeError:
            continue

        def andar(o):
            if alvo:
                return
            if isinstance(o, dict):
                if str(o.get("id")) == anuncio["id"]:
                    ts = _achar_data(o)
                    if ts:
                        alvo["ts"] = ts
                        return
                for v in o.values():
                    andar(v)
            elif isinstance(o, list):
                for v in o:
                    andar(v)
        andar(dados)
        if alvo:
            return alvo["ts"]
    # último recurso: primeiro creation_time da página (normalmente é o do próprio anúncio)
    m = re.search(r'"creation_time"\s*:\s*(\d{10})', r.text)
    return int(m.group(1)) if m else None


def extrair_anuncios(html: str) -> list[dict]:
    """Lê os blocos JSON embutidos na página e devolve anúncios simplificados."""
    achados: dict = {}
    blocos = re.findall(
        r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', html, re.S
    )
    for bloco in blocos:
        if "marketplace_listing_title" not in bloco:
            continue
        try:
            _percorrer(json.loads(bloco), achados)
        except json.JSONDecodeError:
            continue

    return montar_anuncios(achados)


def montar_anuncios(achados: dict) -> list[dict]:
    anuncios = []
    for id_, n in achados.items():
        if n.get("is_sold") or n.get("is_hidden"):
            continue
        preco = n.get("listing_price") or {}
        local = ((n.get("location") or {}).get("reverse_geocode") or {})
        foto = ((n.get("primary_listing_photo") or {}).get("image") or {}).get("uri")
        valor = preco.get("amount")
        try:
            valor = float(valor) if valor is not None else None
        except (TypeError, ValueError):
            valor = None
        anuncios.append({
            "id": id_,
            "titulo": n.get("marketplace_listing_title", "").strip(),
            "preco_txt": preco.get("formatted_amount") or "sem preço",
            "preco": valor,
            "cidade": local.get("city") or "",
            "foto": foto,
            "link": f"https://www.facebook.com/marketplace/item/{id_}/",
            "criado_em": _achar_data(n),
        })
    return anuncios


def parece_bloqueado(html: str, url_final: str) -> bool:
    sinais = ["/login/", "login_form", "checkpoint", "id=\"loginbutton\""]
    return any(s in url_final for s in sinais[:1]) or any(s in html for s in sinais[1:])


# --------------------------------------------------------------------------- #
# Modo SEM LOGIN: API interna (GraphQL) da busca, com latitude/longitude
# --------------------------------------------------------------------------- #
GRAPHQL_URL = "https://www.facebook.com/api/graphql/"
_LSD: dict = {}


def pegar_lsd(sessao: requests.Session) -> str:
    """Token anti-CSRF que a página pública do Facebook entrega para visitantes."""
    if "v" in _LSD:
        return _LSD["v"]
    try:
        html = sessao.get("https://www.facebook.com/marketplace/", headers=HEADERS, timeout=30).text
    except requests.RequestException:
        html = ""
    m = (re.search(r'"LSD",\[\],\{"token":"([^"]+)"', html)
         or re.search(r'name="lsd" value="([^"]+)"', html))
    _LSD["v"] = m.group(1) if m else "AVq5uabXj48"
    return _LSD["v"]


def buscar_graphql(sessao: requests.Session, config: dict, termo: str) -> tuple[list[dict], bool]:
    g = config["sem_login"]
    variaveis = {
        "count": 24,
        "params": {
            "bqf": {"callsite": "COMMERCE_MKTPLACE_WWW", "query": termo},
            "browse_request_params": {
                "commerce_enable_local_pickup": True,
                "commerce_enable_shipping": True,
                "commerce_search_and_rp_available": True,
                "commerce_search_and_rp_condition": None,
                "commerce_search_and_rp_ctime_days": None,
                "commerce_search_sort_by": "CREATION_TIME_DESCEND",
                "filter_location_latitude": g["latitude"],
                "filter_location_longitude": g["longitude"],
                "filter_price_lower_bound": 0,
                "filter_price_upper_bound": 214748364700,
                "filter_radius_km": config.get("raio_km", 10),
            },
            "custom_request_params": {"surface": "SEARCH"},
        },
    }
    lsd = pegar_lsd(sessao)
    headers = dict(HEADERS)
    headers.update({
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://www.facebook.com",
        "Referer": "https://www.facebook.com/marketplace/",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "X-FB-LSD": lsd,
        "X-FB-Friendly-Name": "CometMarketplaceSearchContentPaginationQuery",
    })
    dados = {
        "av": "0", "__user": "0", "__a": "1", "lsd": lsd,
        "fb_api_caller_class": "RelayModern",
        "fb_api_req_friendly_name": "CometMarketplaceSearchContentPaginationQuery",
        "server_timestamps": "true",
        "variables": json.dumps(variaveis),
        "doc_id": str(g["doc_id"]),
    }
    try:
        r = sessao.post(GRAPHQL_URL, headers=headers, data=dados, timeout=30)
    except requests.RequestException as e:
        log(f"  erro de rede: {e}")
        return [], False
    achados: dict = {}
    texto = r.text.removeprefix("for (;;);")
    for linha in texto.splitlines():
        try:
            _percorrer(json.loads(linha), achados)
        except json.JSONDecodeError:
            continue
    anuncios = montar_anuncios(achados)
    if not anuncios:
        log(f"  GraphQL status {r.status_code}: {texto[:250]}")
    return anuncios, (not anuncios and r.status_code in (401, 403))



def buscar(sessao: requests.Session, url: str) -> tuple[list[dict], bool]:
    """Retorna (anúncios, bloqueado?)."""
    try:
        r = sessao.get(url, headers=HEADERS, timeout=30, allow_redirects=True)
    except requests.RequestException as e:
        log(f"  erro de rede: {e}")
        return [], False
    anuncios = extrair_anuncios(r.text)
    bloqueado = not anuncios and (r.status_code != 200 or parece_bloqueado(r.text, r.url))
    if not anuncios:
        log(f"  status {r.status_code}, url final {r.url[:90]}, bloqueio={bloqueado}")
    return anuncios, bloqueado


# --------------------------------------------------------------------------- #
# Filtros
# --------------------------------------------------------------------------- #
def passa_filtro(anuncio: dict, busca: dict, excluir: list[str], cidades: list[str] | None = None) -> bool:
    titulo = normalizar(anuncio["titulo"])
    if cidades and normalizar(anuncio["cidade"]) not in [normalizar(c) for c in cidades]:
        return False
    if not any(normalizar(p) in titulo for p in busca.get("titulo_deve_conter", [])):
        return False
    if any(normalizar(p) in titulo for p in excluir):
        return False
    pmin = busca.get("preco_min")
    if pmin is not None and anuncio["preco"] is not None and anuncio["preco"] < pmin:
        return False
    pmax = busca.get("preco_max")
    if pmax is not None and anuncio["preco"] is not None and anuncio["preco"] > pmax:
        return False
    return True


# --------------------------------------------------------------------------- #
# Notificações
# --------------------------------------------------------------------------- #
# lista de quem mandou /start no bot (carregada do estado.json a cada rodada)
TELEGRAM_ASSINANTES: list[str] = []


def telegram_destinos() -> list[str]:
    """IDs fixos do secret TELEGRAM_CHAT_ID (pode ter vírgulas) + assinantes do /start."""
    fixos = [c.strip() for c in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
    return list(dict.fromkeys(fixos + TELEGRAM_ASSINANTES))


def telegram_responder(token: str, chat: str, texto: str) -> None:
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": texto}, timeout=30)
    except requests.RequestException:
        pass


def atualizar_assinantes(estado: dict) -> None:
    """Lê as mensagens novas do bot: /start inscreve, /parar remove."""
    global TELEGRAM_ASSINANTES
    TELEGRAM_ASSINANTES = [str(c) for c in estado.get("telegram_assinantes", [])]
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        return
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates",
                         params={"offset": estado.get("telegram_offset", 0), "timeout": 0},
                         timeout=30).json()
    except (requests.RequestException, ValueError) as e:
        log(f"  erro lendo mensagens do bot: {e}")
        return
    for u in r.get("result", []):
        estado["telegram_offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        chat = str((msg.get("chat") or {}).get("id", ""))
        texto = (msg.get("text") or "").strip().lower()
        if not chat:
            continue
        nome = (msg.get("chat") or {}).get("first_name", "")
        if texto.startswith("/start"):
            if chat not in TELEGRAM_ASSINANTES:
                TELEGRAM_ASSINANTES.append(chat)
                log(f"  novo assinante no Telegram: {nome} ({chat})")
            telegram_responder(token, chat,
                "✅ Pronto! Você vai receber os alertas de novos anúncios do Marketplace.\n"
                "Para parar, mande /parar.")
        elif texto.startswith(("/parar", "/stop")):
            if chat in TELEGRAM_ASSINANTES:
                TELEGRAM_ASSINANTES.remove(chat)
            telegram_responder(token, chat, "🔕 Você não vai mais receber alertas. Para voltar, mande /start.")
    estado["telegram_assinantes"] = TELEGRAM_ASSINANTES


def enviar(texto: str) -> bool:
    ok = False
    phone, apikey = os.getenv("CALLMEBOT_PHONE"), os.getenv("CALLMEBOT_APIKEY")
    if phone and apikey:
        try:
            r = requests.get(
                "https://api.callmebot.com/whatsapp.php",
                params={"phone": phone, "text": texto, "apikey": apikey},
                timeout=30,
            )
            ok = r.status_code == 200
            log(f"  WhatsApp (CallMeBot): {'ok' if ok else 'FALHOU'}")
            if not ok:
                log(f"  CallMeBot respondeu {r.status_code}: {r.text[:120]}")
        except requests.RequestException as e:
            log(f"  erro CallMeBot: {e}")
        time.sleep(3)  # CallMeBot não gosta de rajadas

    g_id, g_token, g_dest = os.getenv("GREENAPI_ID"), os.getenv("GREENAPI_TOKEN"), os.getenv("GREENAPI_DESTINO")
    if g_id and g_token and g_dest:
        base = (os.getenv("GREENAPI_URL") or "https://api.green-api.com").rstrip("/")
        destino = re.sub(r"\D", "", g_dest)
        try:
            r = requests.post(
                f"{base}/waInstance{g_id}/sendMessage/{g_token}",
                json={"chatId": f"{destino}@c.us", "message": texto},
                timeout=30,
            )
            ok = ok or r.status_code == 200
            if r.status_code != 200:
                log(f"  Green-API respondeu {r.status_code}: {r.text[:150]}")
        except requests.RequestException as e:
            log(f"  erro Green-API: {e}")
        time.sleep(2)

    token = os.getenv("TELEGRAM_TOKEN")
    chats = telegram_destinos()
    for chat in chats if token else []:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat, "text": texto},
                timeout=30,
            )
            ok = ok or r.status_code == 200
            log(f"  Telegram {chat}: {'ok' if r.status_code == 200 else 'FALHOU ' + r.text[:200]}")
            if r.status_code == 403 and chat in TELEGRAM_ASSINANTES:  # bloqueou o bot
                TELEGRAM_ASSINANTES.remove(chat)
        except requests.RequestException as e:
            log(f"  erro Telegram: {e}")
        time.sleep(0.5)

    if not (phone and apikey) and not (token and chats) and not (g_id and g_token and g_dest):
        log("  (nenhum canal configurado — mensagem só no log)")
        print(texto)
    return ok


def fmt_data(ts) -> str:
    if not ts:
        return "data ?"
    from datetime import timedelta
    return datetime.fromtimestamp(ts, timezone(timedelta(hours=-3))).strftime("%d/%m %H:%M")


def formatar(anuncio: dict, nome_busca: str) -> str:
    linhas = [
        f"🔔 {nome_busca} no Marketplace",
        anuncio["titulo"],
        f"💰 {anuncio['preco_txt']}",
    ]
    if anuncio["cidade"]:
        linhas.append(f"📍 {anuncio['cidade']}")
    if anuncio.get("criado_em"):
        linhas.append(f"🕒 publicado {fmt_data(anuncio['criado_em'])}")
    linhas.append(anuncio["link"])
    return "\n".join(linhas)


# --------------------------------------------------------------------------- #
# Rodada principal
# --------------------------------------------------------------------------- #
def rodar(teste: bool = False, enviar_atuais: bool = False) -> None:
    config = carregar_json(ARQ_CONFIG, None)
    if not config:
        sys.exit("config.json não encontrado")
    estado = carregar_json(ARQ_ESTADO, estado_inicial())
    for k, v in estado_inicial().items():
        estado.setdefault(k, v)
    if not teste:
        atualizar_assinantes(estado)

    sessao = requests.Session()
    cookies = os.getenv("FB_COOKIES", "").strip()
    if cookies:
        for par in cookies.split(";"):
            if "=" in par:
                k, v = par.strip().split("=", 1)
                sessao.cookies.set(k, v, domain=".facebook.com")
        log("usando cookies de conta (FB_COOKIES)")
    elif config.get("sem_login", {}).get("ativo"):
        log("modo SEM LOGIN (busca por coordenadas)")

    agora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    limite_data = None
    if config.get("publicado_apos"):
        limite_data = datetime.fromisoformat(config["publicado_apos"]).timestamp()
    idade_max = config.get("idade_maxima_horas")
    enviar_sem_data = config.get("enviar_quando_sem_data", True)

    def recente(a: dict) -> bool:
        ts = a.get("criado_em")
        if ts is None:
            return enviar_sem_data
        if limite_data and ts < limite_data:
            return False
        if idade_max and time.time() - ts > idade_max * 3600:
            return False
        return True

    total_lidos, algum_bloqueio, a_notificar = 0, False, []

    for busca in config["buscas"]:
        nome = busca["nome"]
        encontrados: dict = {}
        lidos_busca = 0
        for termo in busca["termos_busca"]:
            usar_graphql = bool(config.get("sem_login", {}).get("ativo")) and not cookies
            log(f"buscando '{termo}'")
            if usar_graphql:
                anuncios, bloqueado = buscar_graphql(sessao, config, termo)
            else:
                url = montar_url(config["local"], termo, config.get("raio_km", 10))
                anuncios, bloqueado = buscar(sessao, url)
            algum_bloqueio |= bloqueado
            lidos_busca += len(anuncios)
            total_lidos += len(anuncios)
            if teste and anuncios:
                cid = sorted({a["cidade"] for a in anuncios if a["cidade"]})[:6]
                log(f"  cidades nos resultados: {', '.join(cid)}")
            for a in anuncios:
                if passa_filtro(a, busca, config.get("palavras_excluir", []), config.get("cidades_aceitas")):
                    encontrados[a["id"]] = a
            time.sleep(random.uniform(2, 5))

        novos = [a for a in encontrados.values() if a["id"] not in estado["vistos"]]
        log(f"{nome}: {len(encontrados)} relevantes, {len(novos)} novos")

        if teste:
            # no teste, busca a data só dos 3 primeiros (para conferir se o Facebook informa)
            for i, a in enumerate(encontrados.values()):
                if a["criado_em"] is None and i < 3:
                    a["criado_em"] = data_do_anuncio(sessao, a)
                    time.sleep(random.uniform(1, 3))
                marca = "NOVO " if a in novos else "     "
                print(f"   {marca}{a['preco_txt']:>12}  {fmt_data(a['criado_em']):<16}  "
                      f"{a['cidade'][:15]:<15}  {a['titulo'][:50]}  {a['link']}")
            continue

        # data de publicação: só para os novos (poucos por rodada)
        if limite_data or idade_max:
            for a in novos:
                if a["criado_em"] is None:
                    a["criado_em"] = data_do_anuncio(sessao, a)
                    time.sleep(random.uniform(1, 3))
            antes = len(novos)
            novos_recentes = [a for a in novos if recente(a)]
            if antes != len(novos_recentes):
                log(f"  {antes - len(novos_recentes)} ignorados por serem antigos")
        else:
            novos_recentes = novos

        primeira_vez = nome not in estado["buscas_inicializadas"]
        if enviar_atuais:  # manda tudo que está anunciado agora, mesmo o que já foi visto
            a_notificar.extend((a, nome) for a in encontrados.values())
        for a in novos:
            estado["vistos"][a["id"]] = agora
        if not primeira_vez and not enviar_atuais:
            a_notificar.extend((a, nome) for a in novos_recentes)
        if primeira_vez and lidos_busca:
            estado["buscas_inicializadas"].append(nome)
            log(f"  primeira execução de '{nome}': {len(novos)} anúncios antigos marcados como vistos")

    if teste:
        log(f"modo teste: {total_lidos} anúncios lidos no total. Nada foi salvo/enviado.")
        if total_lidos == 0:
            log("NENHUM anúncio lido — provável tela de login/bloqueio. Veja o README (FB_COOKIES).")
        return

    # notificações
    limite = len(a_notificar) if enviar_atuais else MAX_NOTIFICACOES_POR_RODADA
    for a, nome in a_notificar[:limite]:
        enviar(formatar(a, nome))
        time.sleep(1)  # Telegram limita rajadas
    if len(a_notificar) > limite:
        enviar(f"(+{len(a_notificar) - MAX_NOTIFICACOES_POR_RODADA} anúncios novos não enviados nesta rodada)")

    # saúde do monitor
    if total_lidos == 0:
        estado["falhas_seguidas"] += 1
        log(f"nenhum anúncio lido ({estado['falhas_seguidas']} vezes seguidas)")
        if estado["falhas_seguidas"] >= FALHAS_PARA_AVISAR and not estado["aviso_bloqueio_enviado"]:
            motivo = "tela de login/bloqueio" if algum_bloqueio else "página mudou ou erro de rede"
            enviar(f"⚠️ Monitor do Marketplace não está conseguindo ler anúncios ({motivo}). Precisa de ajuste.")
            estado["aviso_bloqueio_enviado"] = True
    else:
        if estado["aviso_bloqueio_enviado"]:
            enviar("✅ Monitor do Marketplace voltou a funcionar.")
        estado["falhas_seguidas"] = 0
        estado["aviso_bloqueio_enviado"] = False

    estado["telegram_assinantes"] = TELEGRAM_ASSINANTES
    salvar_estado(estado)
    log(f"fim: {total_lidos} lidos, {len(a_notificar)} notificados")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--teste", action="store_true", help="não salva nem notifica")
    ap.add_argument("--enviar-atuais", action="store_true",
                    help="envia TODOS os anúncios relevantes que estão no ar agora")
    ap.add_argument("--loop", type=float, metavar="MIN", help="repete a cada MIN minutos")
    ap.add_argument("--testar-whatsapp", action="store_true", help="manda mensagem de teste (WhatsApp/Telegram)")
    ap.add_argument("--descobrir-chat-id", action="store_true", help="mostra o chat id de quem falou com o bot")
    args = ap.parse_args()

    if args.descobrir_chat_id:
        token = os.getenv("TELEGRAM_TOKEN") or input("Token do bot: ").strip()
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=30).json()
        chats = {}
        for u in r.get("result", []):
            msg = u.get("message") or u.get("my_chat_member") or {}
            chat = msg.get("chat") or {}
            if chat.get("id"):
                nome = chat.get("first_name") or chat.get("title") or ""
                chats[chat["id"]] = f"{nome} @{chat.get('username', '')}"
        if not chats:
            print("Nenhuma conversa ainda. Peça para a pessoa abrir o bot, tocar em Iniciar/Start e mandar 'oi'.")
        for cid, nome in chats.items():
            print(f"TELEGRAM_CHAT_ID = {cid}   ({nome})")
        return
    if args.testar_whatsapp:
        global TELEGRAM_ASSINANTES
        estado = carregar_json(ARQ_ESTADO, estado_inicial())
        TELEGRAM_ASSINANTES = [str(c) for c in estado.get("telegram_assinantes", [])]
        print("enviado" if enviar("✅ Teste do monitor do Marketplace") else "falhou")
        return
    if args.loop:
        while True:
            try:
                rodar(teste=args.teste)
            except Exception as e:  # mantém o loop vivo
                log(f"erro inesperado: {e}")
            espera = args.loop * 60 + random.uniform(-30, 30)
            time.sleep(max(60, espera))
    else:
        rodar(teste=args.teste, enviar_atuais=args.enviar_atuais)


if __name__ == "__main__":
    main()
