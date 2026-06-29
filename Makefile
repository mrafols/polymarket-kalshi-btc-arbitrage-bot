COMPOSE_DIR := docker

.PHONY: up down build logs restart clean help

up:
	cd $(COMPOSE_DIR) && docker compose up -d

build:
	cd $(COMPOSE_DIR) && docker compose up --build -d

down:
	cd $(COMPOSE_DIR) && docker compose down

logs:
	cd $(COMPOSE_DIR) && docker compose logs -f

restart:
	cd $(COMPOSE_DIR) && docker compose restart

clean:
	cd $(COMPOSE_DIR) && docker compose down --rmi all --volumes --remove-orphans

help:
	@echo "Usage: make [target]"
	@echo ""
	@echo "Targets:"
	@echo "  up        Start the application"
	@echo "  build     Build and start the application"
	@echo "  down      Stop the application"
	@echo "  logs      View logs (follow mode)"
	@echo "  restart   Restart the application"
	@echo "  clean     Remove everything (containers, images, volumes)"
	@echo "  help      Show this help message"
