# Ollama и Gemma 3

Ollama запускается вместе с проектом через `just up`. Для NVIDIA GPU используйте `just up-gpu`.
Для AMD Radeon на Windows используйте [режим с Ollama и BGE-M3 на хосте](../embeding-service/README.md#запуск-на-amd-radeon-в-windows).
Чтобы запустить только Ollama и загрузку модели, выполните следующие команды из корня проекта.

```powershell
docker compose up -d ollama ollama-init
docker compose logs -f ollama-init
```

При первом запуске `ollama-init` скачивает [gemma3:4b](https://ollama.com/library/gemma3) в том `ollama-model-cache` (около 3,4 ГБ). Когда `ollama-init` завершается с кодом 0, модель готова к запросам. При следующих запусках Ollama использует сохранённые веса. Загрузку можно проверить командой `docker compose exec ollama ollama show gemma3:4b`.

Сервисы Compose обращаются к `http://ollama:11434`. С хоста API доступен по адресу `http://127.0.0.1:11434`. Переменная `OLLAMA_PORT` меняет порт на хосте. Переменная `OLLAMA_MODEL` меняет тег загружаемой модели. Указывайте тот же тег в запросах к API.

```powershell
$body = @{
    model = 'gemma3:4b'
    messages = @(@{ role = 'user'; content = 'Объясни семантический поиск одним предложением.' })
    stream = $false
} | ConvertTo-Json -Depth 4

$response = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:11434/api/chat' `
    -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body))
$response.message.content
```
