FROM python:3-alpine AS base

RUN apk add --update --no-cache gcc g++ musl-dev libffi-dev
RUN pip3 install --user --no-cache acmeasync docker jinja2 pyyaml pydantic

FROM python:3-alpine
# END COMMON

WORKDIR /app
COPY --from=base /root/.local /root/.local

COPY common.py config.py robot.py robocert.py ./

EXPOSE 80/tcp

CMD ["python", "robot.py"]
