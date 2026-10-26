.PHONY: build up down logs logs-ui shell ui open restart

# Build all images (bot + dashboard)
build:
	docker compose build

# Start both bot and dashboard
up:
	docker compose up -d

# Start only dashboard and open in browser
ui:
	docker compose up -d lumibot-dashboard
	@echo "Dashboard running at http://localhost:8501"
	@command -v open > /dev/null 2>&1 && open http://localhost:8501 || command -v xdg-open > /dev/null 2>&1 && xdg-open http://localhost:8501 || echo "Please open http://localhost:8501 manually"

# Open browser to dashboard
open:
	@echo "Dashboard running at http://localhost:8501"
	@command -v open > /dev/null 2>&1 && open http://localhost:8501 || command -v xdg-open > /dev/null 2>&1 && xdg-open http://localhost:8501 || echo "Please open http://localhost:8501 manually"

# Stop everything
down:
	docker compose down

# Tail live logs for trading bot
logs:
	docker compose logs -f lumibot-bot

# Tail live logs for dashboard
logs-ui:
	docker compose logs -f lumibot-dashboard

# Shell into running bot container
shell:
	docker exec -it lumibot-bot bash

# Rebuild + restart
restart: down build up logs
