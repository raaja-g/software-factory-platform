.PHONY: up down image login test-db

up:
	@mkdir -p secrets
	@[ -f secrets/pg_password ] || (umask 077; openssl rand -hex 24 > secrets/pg_password)
	@[ -f secrets/api_token ] || (umask 077; printf 'sfp_%s' "$$(openssl rand -hex 24)" > secrets/api_token)
	@[ -f secrets/broker_token ] || (umask 077; openssl rand -hex 24 > secrets/broker_token)
	@[ -f secrets/github_app_key.pem ] || (umask 077; : > secrets/github_app_key.pem)
	@[ -f secrets/registry-credentials.json ] || (umask 077; echo '{}' > secrets/registry-credentials.json)
	@[ -f .env ] || (umask 077; touch .env)
	docker compose up -d --build --wait
	@docker compose exec api factory login-url

down:
	docker compose down

image:
	docker buildx build --platform linux/amd64,linux/arm64 -f docker/platform.Dockerfile -t swfactory-platform:dev .

login:
	@docker compose exec api factory login-url

test-db:
	docker compose -f compose.yaml -f compose.test.yaml up -d --wait postgres migrate
