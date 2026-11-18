# Alerta de Marketplace → WhatsApp (custo zero)

Verifica o Facebook Marketplace a cada ~10 minutos e manda no WhatsApp cada
anúncio **novo** de PS2, Roçadeira e Xbox 360 em Porto Velho (raio de 10 km).

- Roda de graça no **GitHub Actions** (sem PC ligado).
- WhatsApp grátis via **CallMeBot** (para 1 número).
- Na primeira execução, os anúncios que já existem são só marcados como vistos.
  Depois disso, chegam no WhatsApp apenas os anúncios novos.
- Anúncios com "compro", "procuro" e "busco" no título são ignorados.
- Se o Facebook bloquear por 1 hora seguida, chega um aviso no WhatsApp.

---

## Passo 1 — Conferir o código da cidade

1. No navegador, abra o Marketplace, clique na localização e escolha **Porto Velho, 10 km**.
2. Pesquise qualquer coisa e veja a URL. Ela fica assim:
   `https://www.facebook.com/marketplace/`**`XXXXXXXX`**`/search/?query=...`
3. Se `XXXXXXXX` não for `portovelho`, coloque o valor que aparecer no campo `"local"` do `config.json`.

## Passo 2 — Testar no PC (5 minutos)

```bash
pip install -r requirements.txt
python monitor.py --teste
```

- Se aparecer uma lista de anúncios com preço e link, **funciona sem login**. Pule para o Passo 3.
- Se aparecer `NENHUM anúncio lido`, o Facebook pediu login. Vá para o **Passo 5** e depois volte.

## Passo 3 — Ativar o WhatsApp (CallMeBot, grátis)

1. No celular que vai receber os alertas, siga as instruções em
   <https://www.callmebot.com/blog/free-api-whatsapp-messages/>: salve o número do
   CallMeBot nos contatos e mande a mensagem de autorização que eles pedem.
2. Em alguns minutos chega a **apikey**.
3. Para testar no PC:
   ```bash
   # Windows (PowerShell)
   $env:CALLMEBOT_PHONE="+5569XXXXXXXXX"; $env:CALLMEBOT_APIKEY="123456"
   # Linux/Mac
   export CALLMEBOT_PHONE="+5569XXXXXXXXX" CALLMEBOT_APIKEY="123456"

   python monitor.py --testar-whatsapp
   ```

### Se o CallMeBot estiver "com capacidade máxima": Green-API (plano Developer, grátis)

O plano gratuito da Green-API manda mensagens ilimitadas para até 3 conversas. Para avisar 1 número, é suficiente.

1. Arranje um **chip/número de WhatsApp extra** para ser o remetente (pode ser um chip pré-pago barato
   no celular antigo). A Green-API é uma API não oficial e o número remetente pode ser bloqueado pelo
   WhatsApp, então não use o número principal de ninguém.
2. Crie a conta em <https://green-api.com> e crie uma instância no plano **Developer**.
3. Na instância, escaneie o QR Code com o WhatsApp do número **remetente** (Aparelhos conectados).
4. Anote o `idInstance`, o `apiTokenInstance` e a `apiUrl` que aparecem no console.
5. Configure as variáveis/secrets:
   - `GREENAPI_ID` = idInstance
   - `GREENAPI_TOKEN` = apiTokenInstance
   - `GREENAPI_URL` = apiUrl (ex.: `https://7103.api.greenapi.com`)
   - `GREENAPI_DESTINO` = número que **recebe** os alertas, ex.: `5569999999999`
6. Teste com `python monitor.py --testar-whatsapp`.

### Telegram (grátis e oficial)

1. Crie o bot no @BotFather e guarde o **token**.
2. A pessoa que vai receber os alertas abre o bot (`t.me/NOME_DO_BOT`), toca em **Iniciar** e manda "oi".
3. Rode `python monitor.py --descobrir-chat-id` e cole o token. O comando mostra o `TELEGRAM_CHAT_ID`.
   Sem o script, dá para abrir no navegador `https://api.telegram.org/bot<TOKEN>/getUpdates` e procurar `"chat":{"id": ...`.
4. Crie os secrets `TELEGRAM_TOKEN` e `TELEGRAM_CHAT_ID` e teste com `python monitor.py --testar-whatsapp`.

> Outra opção é um bot do Telegram. Crie o bot no @BotFather e
> configure `TELEGRAM_TOKEN` e `TELEGRAM_CHAT_ID`. Dá para usar os dois canais ao mesmo tempo.

## Passo 4 — Subir no GitHub Actions

1. Crie um repositório no GitHub e envie esta pasta inteira, incluindo `.github/`.
   - **Repositório público** = minutos ilimitados, dá para rodar a cada 10 min. As senhas ficam
     em *Secrets* e não aparecem no repositório.
   - **Repositório privado** = cota mensal de minutos. Nesse caso, troque o cron para
     `*/30 * * * *` no arquivo `.github/workflows/monitor.yml`.
2. Vá em **Settings → Secrets and variables → Actions → New repository secret** e crie:
   - `CALLMEBOT_PHONE` (ex.: `+5569999999999`)
   - `CALLMEBOT_APIKEY`
   - ou, se usar Green-API: `GREENAPI_ID`, `GREENAPI_TOKEN`, `GREENAPI_URL`, `GREENAPI_DESTINO`
   - `FB_COOKIES` (só se precisar, veja o Passo 5)
3. Na aba **Actions**, habilite os workflows, abra "Monitor Marketplace" e clique em **Run workflow**
   para a primeira execução.
4. Abra o log da execução. A linha `fim: N lidos` precisa ter **N > 0**. Se for 0, o GitHub
   está sendo bloqueado: veja o Passo 5 ou o Passo 6.

## Passo 5 — Se pedir login: usar uma conta secundária

> Nunca use a conta principal. Robô com conta do Facebook é contra os termos
> de uso, e a conta secundária pode ser bloqueada. Se isso acontecer, é só criar outra.

1. Crie a conta nova, use normalmente pelo navegador por alguns dias e defina a localização em Porto Velho.
2. Logado nessa conta, aperte **F12 → Application → Cookies → https://www.facebook.com**.
3. Copie os valores de `c_user`, `xs` e `datr` e monte assim:
   `c_user=1000...; xs=12%3Aabc...; datr=AbCd...`
4. Salve esse texto no secret `FB_COOKIES`. Para testar no PC, coloque na variável de ambiente
   `FB_COOKIES` e rode `--teste`.
5. Se você sair da conta no navegador (logout), os cookies param de valer. Feche a aba sem deslogar.

## Passo 6 — Se o GitHub for bloqueado mesmo com cookies: celular Android

Um celular antigo ligado no Wi-Fi da casa usa IP residencial, que o Facebook bloqueia bem menos.

1. Instale o **Termux** pelo F-Droid.
2. No Termux:
   ```bash
   pkg install python git
   git clone https://github.com/SEU_USUARIO/SEU_REPO.git && cd SEU_REPO
   pip install -r requirements.txt
   export CALLMEBOT_PHONE="+5569..." CALLMEBOT_APIKEY="..."   # e FB_COOKIES se precisar
   termux-wake-lock
   python monitor.py --loop 5
   ```
3. Deixe o celular carregando e desative a otimização de bateria do Termux.

---

## Ajustes no `config.json`

| Campo | O que faz |
|---|---|
| `local` / `raio_km` | Cidade (código da URL) e raio em km |
| `termos_busca` | O que é digitado na busca do Marketplace |
| `titulo_deve_conter` | O título precisa ter pelo menos uma dessas palavras (sem acento, minúsculo). Evita resultados "parecidos" que o Facebook mistura |
| `palavras_excluir` | Ignora o anúncio se o título tiver alguma dessas palavras |
| `preco_max` | Ex.: `400` para avisar só até R$ 400. `null` = sem limite |

## Manutenção

- Quando o Facebook muda o site, o aviso "⚠️ não está conseguindo ler anúncios" chega no WhatsApp.
  Rode `python monitor.py --teste` para ver o que mudou.
- Repositórios públicos sem nenhuma atividade por 60 dias podem ter o agendamento pausado pelo GitHub.
  Os commits automáticos do `estado.json` normalmente evitam isso. Se pausar, é só reativar na aba Actions.
