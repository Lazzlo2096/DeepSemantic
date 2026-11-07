# Сервис эмбеддингов BGE-M3

В Compose работают три сервиса. `api` принимает HTTP-запросы, `rebbit` запускает RabbitMQ, а `embeding-service` загружает BGE-M3 и выполняет инференс.

Из корня репозитория выполните `just up` или `docker compose up --build --detach`. Swagger UI доступен на http://127.0.0.1:8000/docs после запуска API. Порт на хосте можно изменить переменной `EMBEDDING_PORT`.

При первом запуске воркер скачивает около 2,3 ГБ весов [BAAI/bge-m3](https://huggingface.co/BAAI/bge-m3) в том `embedding-model-cache`. `just logs` показывает журналы всех сервисов, `just ps` — состояние контейнеров. `just down` сохраняет тома модели и RabbitMQ.

## Запуск на NVIDIA GPU

`just up` использует CPU. На машине с NVIDIA GPU запустите `just up-gpu`. Этот режим даёт воркеру доступ к GPU, устанавливает PyTorch с CUDA 12.8 и запускает BGE-M3 на `cuda:0` в FP16. Для Docker Desktop на Windows нужны [WSL 2 и драйвер NVIDIA с поддержкой GPU](https://docs.docker.com/desktop/features/gpu/).

Проверить, видит ли PyTorch видеокарту внутри воркера, можно командой `docker compose exec embeding-service python -c "import torch; print(torch.cuda.is_available())"`.

## Запуск на AMD Radeon в Windows

В этом режиме Ollama и воркер BGE-M3 запускаются в Windows, а API, RabbitMQ и остальные сервисы остаются в Docker Compose. Для Ollama используется Vulkan, для BGE-M3 — PyTorch с ROCm. Для Ryzen AI 9 465 с Radeon 880M AMD указывает поддержку PyTorch 2.9.1 с ROCm 7.2.1 на Windows 11. Сверьте версию драйвера и установите пакеты ROCm и PyTorch в отдельное окружение Python 3.12. [Матрица AMD](https://rocm.docs.amd.com/projects/radeon-ryzen/en/latest/docs/compatibility/compatibilityryz/windows/windows_compatibility.html) · [Инструкция AMD](https://rocm.docs.amd.com/projects/radeon-ryzen/en/latest/docs/install/installryz/windows/install-pytorch.html)

1. Установите [Ollama для Windows](https://docs.ollama.com/windows). Если приложение уже запущено в трее, завершите его. Освободите порт 11434 командой `docker compose stop ollama` из корня проекта. Затем запустите сервер в отдельном PowerShell. [Поддержка Vulkan в Ollama](https://docs.ollama.com/gpu#vulkan-gpu-support)

   ```powershell
   $env:OLLAMA_HOST = '127.0.0.1:11434'
   $env:OLLAMA_VULKAN = '1'
   $env:OLLAMA_IGPU_ENABLE = '1'
   ollama serve
   ```

2. В PowerShell из корня репозитория запустите `just up-amd`. Compose проверит доступ к Ollama из контейнера и загрузит `OLLAMA_MODEL` (по умолчанию `gemma3:4b`) в хранилище Ollama на Windows.

3. В PowerShell с активным окружением ROCm выполните следующие команды из корня репозитория. PyTorch использует имя устройства `cuda:0` и для бэкенда ROCm. Проверка печатает название GPU и выполняет на нём простое вычисление.

   ```powershell
   python -m pip install -r embeding-service/requirements.txt
   python -c "import torch; assert torch.version.hip and torch.cuda.is_available(), 'ROCm GPU unavailable'; print(torch.cuda.get_device_name(0), torch.ones(1, device='cuda:0').sum().item())"
   if ($LASTEXITCODE -ne 0) { throw 'ROCm GPU unavailable' }
   $env:EMBEDDING_DEVICE = 'cuda:0'
   $env:AMQP_URL = 'amqp://deepsemantic:deepsemantic@127.0.0.1:5672/'
   python embeding-service/main.py
   ```

   Если меняли учётные данные RabbitMQ, укажите их в `AMQP_URL` воркера. При первом запуске BGE-M3 скачивается в кэш Hugging Face на Windows. Дождитесь сообщения `is ready and consuming embeddings.requests`, затем проверьте `POST /embeddings`.

Для проверки Ollama выполните `ollama run gemma3:4b "Ответь одним словом: работает?"`, затем `ollama ps`. Столбец `PROCESSOR` показывает, какая часть модели загружена на GPU. [Проверка в Ollama](https://docs.ollama.com/faq#how-can-i-tell-if-my-model-was-loaded-onto-the-gpu)

Чтобы вернуться к контейнерным воркерам, завершите оба процесса на Windows и запустите `just up` или `just up-gpu` на машине с NVIDIA.

## API и очередь

`POST /embeddings` принимает от 1 до 32 текстов. API публикует задание в очередь `embeddings.requests`. Воркер отправляет результат в очередь ответов API с тем же идентификатором запроса. HTTP-ответ содержит `model`, `revision`, `dimensions` и массив нормализованных векторов размерности 1024 в порядке входных текстов.

```powershell
$body = @{ texts = @('Как построить эмбеддинг?', 'BGE-M3 кодирует текст в вектор.') } | ConvertTo-Json -Compress
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/embeddings `
    -ContentType 'application/json; charset=utf-8' -Body $body
```

- `GET /health` возвращает `200`, когда RabbitMQ видит готового потребителя очереди.
- Пока воркер загружает модель или брокер недоступен, `/health` и корректные запросы к `/embeddings` возвращают `503`.
- Если воркер не ответил за установленное время, API возвращает `504`. Просроченные задания удаляются из очереди. Уже начатый инференс может завершиться после таймаута.
- Пустые строки и превышение лимита текста возвращают `422`. При превышении лимита токенов `detail.text_index` содержит индекс текста с нуля.

| Переменная Compose | По умолчанию | Назначение |
| --- | --- | --- |
| `EMBEDDING_MAX_TOKENS` | `512` | Лимит на текст со специальными токенами, от 3 до 8192 |
| `EMBEDDING_BATCH_SIZE` | `8` | Число текстов в одном батче модели, от 1 до 32 |
| `EMBEDDING_RPC_TIMEOUT` | `60` | Время ожидания ответа по AMQP в секундах |

На хосте RabbitMQ принимает AMQP-подключения на `127.0.0.1:5672`. Панель управления открывается по адресу http://127.0.0.1:15672. Локальные логин и пароль по умолчанию — `deepsemantic`. Сервисы внутри Compose подключаются к `rebbit:5672`. Для своего брокера задайте `AMQP_URL` для обоих сервисов.
