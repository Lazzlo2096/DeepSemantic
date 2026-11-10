# DeepSemantic

## Развёртывание на Ubuntu

Выполни на сервере от имени `root`.

```sh
git clone --depth 1 https://github.com/Lazzlo2096/DeepSemantic.git
cd DeepSemantic

chmod +x ./bootstrap.sh
./bootstrap.sh
```

После установки укажи в `.env` ключ `OPENROUTER_API_KEY` и выбери модели `RAG_OPENROUTER_MODEL` и `SUMMARY_OPENROUTER_MODEL`. Для эмбеддингов через OpenRouter шаблон задаёт `EMBEDDING_PROVIDER=openrouter` и `EMBEDDING_OPENROUTER_MODEL=baai/bge-m3`. Запускай и останавливай проект из каталога `DeepSemantic`.

```sh
just up-server # запустить проект
just down      # остановить проект
```
