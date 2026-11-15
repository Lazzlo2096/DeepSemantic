from collections.abc import Awaitable, Callable

import httpx

from llm_chat import ChatResponseError, ChatSettings


FINAL_INSTRUCTIONS = """Ты — научный редактор, который объясняет исследования любознательному программисту.

Прочитай статью целиком и напиши на русском языке объяснение, после которого читатель поймёт не только ЧТО обнаружили авторы, но и ПОЧЕМУ это интересно.

Структура:
1. Главная идея: 2–3 предложения простым языком.
2. Проблема: какой вопрос пытались решить авторы и почему это важно?
3. Метод: как работает предложенный подход? Обязательно приведи конкретный пример.
4. Эксперименты: расскажи о ключевых результатах. Используй числа только там, где они помогают понять результат.
5. Ограничения: что результаты НЕ доказывают?
6. Значение: где и зачем это потенциально можно применить?

Требования:
- Пиши исключительно по-русски.
- Не копируй формулировки из английской статьи.
- Объясняй специальные термины при первом появлении.
- Не превращай ответ в перечень статистических показателей.
- Не добавляй фактов, которых нет в статье.
- Различай наблюдения, гипотезы авторов и доказанные выводы.
- Объём: 700–1000 слов.

Статья:

"""


class SummaryError(Exception):
    pass


class SummaryInputError(SummaryError):
    pass


class Summarizer:
    def __init__(self, client: httpx.AsyncClient, chat: ChatSettings):
        self.client = client
        self.chat = chat

    async def summarize(self, source: str, progress: Callable[[int, int], Awaitable[None]]) -> str:
        prompt = FINAL_INSTRUCTIONS + source
        await progress(0, 1)
        try:
            summary = await self.chat.complete(
                self.client, [{"role": "user", "content": prompt}],
                max_tokens=None,
            )
        except httpx.HTTPStatusError as error:
            message = error.response.text.lower()
            if "context" in message and any(word in message for word in ("exceed", "too long", "too large", "overflow")):
                raise SummaryInputError(f"Полный запрос превышает рабочий контекст {self.chat.provider}.") from error
            raise
        except ChatResponseError as error:
            raise SummaryError(f"{self.chat.provider}: {error}.") from error
        except ValueError as error:
            raise SummaryError(f"{self.chat.provider} вернул неполное или пустое саммари.") from error
        await progress(1, 1)
        return summary
