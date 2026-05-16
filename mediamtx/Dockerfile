FROM ubuntu:22.04

RUN apt-get update && apt-get install -y \
    ffmpeg \
    wget \
    && rm -rf /var/lib/apt/lists/* \
    && localedef -i en_US -c -f UTF-8 -A /usr/share/locale/locale.alias en_US.UTF-8

ENV LANG=en_US.utf8

WORKDIR /app
RUN wget https://go.dev/dl/go1.23.2.linux-amd64.tar.gz
RUN rm -rf /usr/local/go && tar -C /usr/local -xzf go1.23.2.linux-amd64.tar.gz
ENV PATH=$PATH:/usr/local/go/bin
RUN go version

RUN git clone https://github.com/bluenviron/mediamtx.git /mediamtx

WORKDIR /mediamtx
RUN go generate ./...
RUN CGO_ENABLED=0 go build .

CMD ["./mediamtx"]