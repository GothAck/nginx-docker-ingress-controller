build:
	docker build --tag gothack/docker-swarm-ingress:controller-latest -f Dockerfile.controller .
	docker build --tag gothack/docker-swarm-ingress:robot-latest -f Dockerfile.robot .
	docker build --tag gothack/docker-swarm-ingress:nginx-latest -f Dockerfile.nginx .

push:
	docker push gothack/docker-swarm-ingress:controller-latest
	docker push gothack/docker-swarm-ingress:robot-latest
	docker push gothack/docker-swarm-ingress:nginx-latest

build_clean:
	docker builder prune -f --all

upload: build
	echo "TODO: Upload images"

deploy:
	docker network create --attachable --driver overlay --opt encrypted nginx-docker-ingress || true
	docker service create \
		--name nginx-docker-ingress-controller \
		--replicas 1 \
		--mount type=bind,source=/var/run/docker.sock,destination=/var/run/docker.sock \
		--constraint node.role==manager \
		gothack/docker-swarm-ingress:controller-latest

teardown:
	docker service rm nginx-docker-ingress-controller || true
	docker service rm nginx-docker-ingress-nginx || true
	docker service rm nginx-docker-ingress-robot || true
	docker service rm nginx-docker-ingress-account || true

teardown_certs:
	@docker secret ls --format "{{.Name}}" | grep -E "^ndi.svc" | xargs docker secret rm || true

teardown_confs:
	@docker secret ls --format "{{.Name}}" | grep -E "^ndi.conf" | xargs docker secret rm || true

clean:
	docker image rm gothack/docker-swarm-ingress:controller-latest || true
	docker image rm gothack/docker-swarm-ingress:robot-latest || true
	docker image rm gothack/docker-swarm-ingress:nginx-latest || true
	docker image prune -f
