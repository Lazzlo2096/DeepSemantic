# Сервис эмбеддингов BGE-M3

Сервис локально строит нормализованные плотные эмбеддинги размерности 1024. При первом запуске он скачивает закреплённую версию [BAAI/bge-m3](https://huggingface.co/BAAI/bge-m3) с Hugging Face. Последующие запуски используют кеш. Тексты обрабатываются на машине, где запущен сервис.

## Запуск на Windows в PowerShell

Из корня репозитория, с установленным Python 3.11:

```powershell
cd embeding-service
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --index-url https://download.pytorch.org/whl/cpu torch==2.7.1
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn main:app --port 8000
```

Для CUDA установите [сборку PyTorch с поддержкой CUDA](https://pytorch.org/get-started/locally/) вместо CPU-сборки и задайте `$env:EMBEDDING_DEVICE = "cuda:0"` перед запуском. Сервис использует FP32 на CPU и FP16 на CUDA. Каждый процесс Uvicorn загружает свою копию модели.

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `EMBEDDING_DEVICE` | `cpu` | Устройство PyTorch |
| `EMBEDDING_MAX_TOKENS` | `512` | Лимит на текст со специальными токенами, от 3 до 8192 |
| `EMBEDDING_BATCH_SIZE` | `8` | Число текстов в одном батче модели, от 1 до 32 |

## API

`GET /health` возвращает `{"status":"ok"}` после загрузки модели. Интерактивная документация доступна по адресу `http://127.0.0.1:8000/docs`.

```powershell
$body = @{ texts = @('Как построить эмбеддинг?', 'BGE-M3 кодирует текст в вектор.') } | ConvertTo-Json -Compress
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/embeddings `
    -ContentType 'application/json; charset=utf-8' -Body $body
```

`POST /embeddings` принимает от 1 до 32 текстов длиной до 32 768 символов каждый. Ответ содержит `model`, `revision`, `dimensions` и массив `embeddings` в порядке входных текстов. Пустые строки и входы длиннее лимита возвращают HTTP 422. При превышении лимита токенов `detail.text_index` содержит индекс текста с нуля. Разбивайте длинные документы на фрагменты на стороне клиента.
