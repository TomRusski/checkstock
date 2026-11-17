.PHONY: help up down reset-db logs ps seed test test-cov lint fmt demo invariants doctor ports migrate psql

help:            ## Показать доступные команды
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

up:              ## Поднять БД и приложение
	docker compose up -d --build
	@port=$$(grep -E '^APP_PORT=' .env 2>/dev/null | cut -d= -f2); \
	 echo "Готово: http://localhost:$${port:-18080}"

down:            ## Остановить контейнеры (данные сохраняются)
	docker compose down

reset-db:        ## Остановить и УДАЛИТЬ том с данными (чистый старт)
	docker compose down -v

logs:            ## Логи приложения
	docker compose logs -f app

ps:              ## Статус контейнеров
	docker compose ps

migrate:         ## Применить миграции
	docker compose exec app alembic upgrade head

seed:            ## Пересоздать тестовые данные из ТЗ (удаляет текущие!)
	docker compose exec app python -m app.seed --force

test:            ## Прогнать все тесты внутри контейнера
	docker compose exec app pytest

test-cov:        ## Тесты с отчётом о покрытии
	docker compose exec app pytest --cov=app --cov-report=term-missing

lint:            ## Проверка стиля
	docker compose exec app ruff check app tests scripts

fmt:             ## Автоформатирование
	docker compose exec app ruff format app tests scripts
	docker compose exec app ruff check --fix app tests scripts

demo:            ## Сквозной сценарий из ТЗ против запущенного сервера
	docker compose exec app python scripts/demo_scenario.py --base-url http://localhost:8000

invariants:      ## Проверить сходимость остатков
	docker compose exec app python scripts/check_invariants.py --base-url http://localhost:8000

doctor:          ## Всё ли живо: БД, API, сходимость остатков
	@docker compose ps
	docker compose exec app python -c "import httpx; print('API:', httpx.get('http://localhost:8000/api/health').json())"
	docker compose exec app python scripts/check_invariants.py --base-url http://localhost:8000

ports:           ## Показать итоговые порты (с учётом .env)
	@docker compose config | grep -E 'published|target: [0-9]+$$'

psql:            ## Консоль PostgreSQL
	docker compose exec db psql -U checkstock -d checkstock
