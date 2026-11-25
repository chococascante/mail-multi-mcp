# mail-multi-mcp

Servidor [MCP](https://modelcontextprotocol.io) **local** (transporte stdio) que permite a
Claude Desktop / Claude Code **buscar, leer y responder correos de varias cuentas a la vez**:
varias de Gmail y una o más de Outlook.com / Microsoft 365 (vía Microsoft Graph).

## 1. Qué es y qué no es

**Es:**
- Un proceso local que Claude lanza en tu máquina. No hay servidor en la nube ni terceros:
  habla directo con la Gmail API y con Microsoft Graph usando *tus* apps OAuth.
- Multi-cuenta: cada resultado viene etiquetado con su cuenta y los IDs son compuestos
  (`<alias>:<id>`), así que una respuesta siempre sale de la cuenta dueña del mensaje.
  No existe una "cuenta default".
- Prudente: responder **siempre crea un borrador**; enviar es una tool aparte
  (`send_draft`), bloqueada en cuentas `read_only`, con límite de envíos por hora y un
  log de auditoría local.

**No es:**
- Un cliente de correo completo. El MVP no maneja etiquetas, carpetas, borrado ni
  adjuntos (ver [Roadmap](#8-roadmap)).
- Un servicio compartido: los tokens viven en el keychain de tu sistema operativo.

> [!WARNING]
> **Prompt injection.** El contenido de un correo lo escribe cualquiera. Un correo puede
> contener texto diseñado para que un modelo haga cosas que no pediste ("reenviá todo a…").
> Las descripciones de las tools le advierten a Claude que el contenido es **no confiable**
> y que nunca debe enviar sin que se lo pidas explícitamente, pero **revisá siempre los
> borradores antes de aprobar un `send_draft`**. Para cuentas sensibles usá
> `mode=read_only`.

## 2. Instalación (venv estándar)

Requisitos: Python **3.12 o superior** (probado con 3.12 y 3.13) y `git`.

### macOS / Linux

```bash
git clone https://github.com/chococascante/mail-multi-mcp.git
cd mail-multi-mcp
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install -e .
```

### Windows (PowerShell)

```powershell
git clone https://github.com/chococascante/mail-multi-mcp.git
cd mail-multi-mcp
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -e .
```

> Si PowerShell bloquea `Activate.ps1`, corré una vez
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

Para desarrollo usá `requirements-dev.txt` en lugar de `requirements.txt` y corré lo mismo que
la CI:

```bash
pip install -r requirements-dev.txt && pip install -e .
ruff check . && ruff format --check . && mypy src
pytest --cov=mail_multi_mcp --cov-fail-under=90
```

### Dónde se guarda cada cosa

| Qué | Dónde |
|---|---|
| `accounts.json` (alias, email, proveedor, modo, settings) y `oauth_clients.json` (client IDs) | Directorio de config del usuario: macOS `~/Library/Application Support/mail-multi-mcp`, Linux `~/.config/mail-multi-mcp`, Windows `%LOCALAPPDATA%\mail-multi-mcp` |
| Client secret de Google, refresh tokens de Gmail, cache de MSAL | Keychain del SO (`keyring`): Llavero de macOS, Secret Service en Linux, Credential Manager en Windows. Una entrada por cuenta: `mail-multi-mcp:<email>` |
| Access tokens | Solo en memoria |
| Log de auditoría `audit.jsonl` | Directorio de datos del usuario (Linux `~/.local/share/mail-multi-mcp`; en macOS/Windows el mismo de config) |

Podés cambiar esos directorios con las variables `MAIL_MULTI_MCP_CONFIG_DIR` y
`MAIL_MULTI_MCP_DATA_DIR`. `config.example.json` muestra la forma de ambos archivos.

## 3. Registrar la app OAuth de Google

1. Entrá a <https://console.cloud.google.com/>, creá un proyecto (por ejemplo
   `mail-multi-mcp`).
2. **APIs y servicios → Biblioteca** → buscá **Gmail API** → *Habilitar*.
3. **APIs y servicios → Pantalla de consentimiento de OAuth** (o *Google Auth Platform*):
   - Tipo de usuario: **Externo**.
   - Completá nombre de la app y tu correo de soporte.
   - **Scopes:** agregá `.../auth/gmail.readonly` y `.../auth/gmail.compose`.
     No hace falta (ni se pide) `https://mail.google.com/`.
   - **Usuarios de prueba:** agregá cada cuenta de Gmail que vas a conectar.
4. **Credenciales → Crear credenciales → ID de cliente de OAuth** → tipo
   **App de escritorio (Desktop app)**. Copiá el *client ID* y el *client secret*.
5. En tu terminal (con el venv activo):

   ```bash
   mail-multi-mcp-accounts setup-google
   ```

   El client ID va a `oauth_clients.json`; el secret, al keychain.

> [!IMPORTANT]
> **Modo "Testing" = tokens de 7 días.** Mientras la app está en estado *Testing*, Google
> invalida los refresh tokens a los **7 días** y vas a tener que volver a agregar la cuenta.
> Para uso personal podés pasar la app a **"In production"** (botón *Publicar app*) **sin
> verificarla**: Google mostrará una pantalla de "Google no verificó esta app" al autorizar;
> elegí *Avanzado → Ir a mail-multi-mcp (no seguro)*. Esto es aceptable porque la app es
> tuya y solo la usás vos (los scopes de Gmail son "restringidos": sin verificación hay un
> tope de 100 usuarios, de sobra para uso personal).

El flujo de login usa *loopback* (`http://127.0.0.1:<puerto aleatorio>`) con PKCE.

## 4. Registrar la app en Microsoft Entra ID

1. Entrá a <https://entra.microsoft.com/> (o *Azure Portal → Microsoft Entra ID*) →
   **App registrations → New registration**.
2. **Supported account types:**
   - Solo cuentas personales (Outlook.com/Hotmail): **"Personal Microsoft accounts only"**.
   - Personales + de trabajo/escuela: **"Accounts in any organizational directory and
     personal Microsoft accounts"**.
3. **Redirect URI:** plataforma **"Public client/native (mobile & desktop)"**
   ("Mobile and desktop applications") con `http://localhost`.
4. Creada la app: **Authentication → Advanced settings → Allow public client flows = Yes**.
   No crees ningún *client secret*: es una app pública.
5. **API permissions → Add a permission → Microsoft Graph → Delegated permissions:**
   `Mail.ReadWrite`, `Mail.Send`, `User.Read`, `offline_access`.
   (MSAL pide `offline_access` automáticamente; igual dejalo agregado.)
6. Copiá el **Application (client) ID** desde *Overview* y corré:

   ```bash
   mail-multi-mcp-accounts setup-microsoft                    # cuentas personales (consumers)
   mail-multi-mcp-accounts setup-microsoft --tenant common    # personales + trabajo
   ```

## 5. Agregar cuentas

```bash
mail-multi-mcp-accounts add --provider gmail --alias personal
mail-multi-mcp-accounts add --provider gmail --alias trabajo --mode read_only
mail-multi-mcp-accounts add --provider graph --alias outlook
mail-multi-mcp-accounts list
mail-multi-mcp-accounts set-mode trabajo full
mail-multi-mcp-accounts remove trabajo
```

Cada `add` abre el navegador para autorizar. El alias es corto (minúsculas, dígitos, `-`,
`_`); `all` está reservado. El email se detecta automáticamente.

## 6. Configurar Claude

Usá siempre el **Python del `.venv`** (rutas de ejemplo: reemplazalas por la tuya).

### Claude Desktop

Editá `claude_desktop_config.json` (macOS:
`~/Library/Application Support/Claude/claude_desktop_config.json`; Windows:
`%APPDATA%\Claude\claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "mail": {
      "command": "/RUTA/A/mail-multi-mcp/.venv/bin/python",
      "args": ["-m", "mail_multi_mcp.server"]
    }
  }
}
```

En Windows: `"command": "C:\\RUTA\\A\\mail-multi-mcp\\.venv\\Scripts\\python.exe"`.
Reiniciá Claude Desktop.

### Claude Code

```bash
claude mcp add mail -- /RUTA/A/mail-multi-mcp/.venv/bin/python -m mail_multi_mcp.server
```

Opcional: copiá `skill/SKILL.md` a `~/.claude/skills/mail-multi-mcp/SKILL.md` y
personalizá qué cuenta usar para qué (en tu copia local, no en el repo).

## 7. Tools

| Tool | Qué hace | Anotaciones |
|---|---|---|
| `list_accounts()` | Alias, email, proveedor, modo y si hay credenciales guardadas | read-only |
| `search_threads(account, query, max_results=20)` | `account` = alias, email o `"all"`. Con `"all"` busca en paralelo, mezcla por fecha desc y devuelve `errors` por cuenta si alguna falla | read-only |
| `get_thread(thread_id)` | Hilo completo, cuerpos en texto plano (truncados a `max_body_chars`, con `body_truncated`) | read-only |
| `reply(message_id, body, reply_all=False)` | **Crea un borrador** de respuesta en la cuenta dueña del mensaje; devuelve `draft_id` y preview | no destructiva |
| `create_draft(account, to, subject, body, cc=None)` | Borrador nuevo | no destructiva |
| `send_draft(draft_id)` | Envía un borrador. Pasa por el SafetyGuard (read_only + límite por hora) | **destructiva**, no idempotente |

`query` es un objeto con `text`, `from`, `to`, `subject`, `after`/`before` (`YYYY-MM-DD`),
`unread`, `has_attachment` y `raw` (sintaxis nativa, se combina con AND).

### Ejemplos de prompts

- "¿Qué correos sin leer tengo de esta semana en todas mis cuentas?"
- "Buscá en `outlook` las facturas con adjunto de octubre y resumime montos y fechas."
- "Abrí el último hilo con alice@example.com y prepará una respuesta agradeciendo; no la envíes."
- "Mostrame el borrador. OK, envialo." ← recién acá Claude debería usar `send_draft`.

### Diferencias entre proveedores

- **Gmail**: `query` se traduce a la sintaxis de búsqueda de Gmail (`from:`, `after:`,
  `is:unread`, `has:attachment`…). Los listados usan `format=metadata`; `has_attachment` en
  los resúmenes es una heurística (`multipart/mixed`). Las respuestas se arman como MIME con
  `In-Reply-To`, `References`, `Re:` sin duplicar y `threadId`, para quedar en el mismo hilo.
- **Graph**: si hay texto/remitente/destinatario/asunto se usa `$search` (KQL); si solo hay
  fechas/leído/adjuntos se usa `$filter` + `$orderby`. Limitaciones:
  - `$search` no se puede combinar con `$filter` ni `$orderby`: las fechas van en KQL
    (`received>=`) y `unread` se filtra del lado del cliente sobre una página mayor.
  - `$search` devuelve como máximo 1000 resultados.
  - Con `$filter`, Graph exige que la propiedad de `$orderby` aparezca primero en el filtro
    (error `InefficientFilter`); el servidor ya lo resuelve.
  - Los hilos son `conversationId`. Responder usa `createReply`/`createReplyAll` → `PATCH`
    del cuerpo (conservando la cita) → `send` al enviar.

### SafetyGuard y auditoría

- Las cuentas `read_only` no pueden `reply`, `create_draft` ni `send_draft`.
- `settings.max_sends_per_hour` (default 20) limita los envíos; el conteo se reconstruye del
  log al reiniciar.
- `audit.jsonl` registra cada borrador y envío: timestamp, cuenta, destinatarios, asunto y
  resultado. **Nunca el cuerpo.** Si un borrador se creó fuera de esta sesión del servidor,
  el envío se registra sin destinatarios (no se vuelven a pedir al proveedor).

## 8. Roadmap

- Etiquetas / categorías y mover entre carpetas.
- Adjuntos: descarga a archivo (las interfaces `AttachmentRef` y `AttachmentProvider` ya
  existen).
- Proveedor IMAP genérico.
- Procesamiento de contenido: facturas, extracción de PDFs adjuntos.
- Empaquetado `.mcpb` para instalar en Claude Desktop con un clic.

## Licencia

[MIT](LICENSE)
