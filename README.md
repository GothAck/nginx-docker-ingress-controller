# Nginx "ingress controller" with Let's Encrypt for Docker Swarm

## Why?
Because k8s is waaaaay too much for some projects, Docker Swarm just works.

## What?

Run this service in your Swarm, label your frontend services, magic happens.

Seriously:
```sh

cat > my.config.yaml <<EOF
acme:
  email: my-email@example.com
  accept_tos: True
EOF

docker config create ndi.config.0 my.config.yaml

docker network create --driver overlay --opt encrypted nginx-docker-ingress || true
docker service create \
        --name nginx-docker-ingress-controller \
        --replicas 1 \
        --mount type=bind,source=/var/run/docker.sock,destination=/var/run/docker.sock \
        --constraint node.role==manager \
        gothack/docker-swarm-ingress:controller-latest

docker service update my-http-frontend \
        --label-add nginx-ingress.host=example.com,subdomain.example.com \
        --label-add nginx-ingress.ssl \
        --label-add nginx-ingress.ssl-redirect

# Make a cup of tea

curl http://example.com
```

## But k8s?!?!?!
Yeah, I cba running my own bare-metal k8s cluster... <shrug />

## TODO:
- Code tidy
- Handle exceptions better
- Handle async tasks failing
- Tests
- Make more tea
- Tidy up old nginx configs & key/cert pairs
- Utilize Docker Configs for more non-secret data storage
- X-Forwarded-For or whatever
