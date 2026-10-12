FROM debian:bookworm-slim AS build
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
# Pin source content; do not build native CPU instructions from the build host.
RUN curl -fsSL https://codeload.github.com/ggml-org/llama.cpp/tar.gz/8345f333951c661d166b00e6f9362e553768f292 -o source.tar.gz \
    && echo '84050e783dbd0c4a4678f90f253b0881df04e90a98c20f73ae3709751c4a4850  source.tar.gz' | sha256sum -c - \
    && tar -xzf source.tar.gz --strip-components=1
RUN cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
    -DGGML_NATIVE=OFF -DGGML_AVX512=OFF -DGGML_OPENMP=OFF \
    -DBUILD_SHARED_LIBS=OFF -DLLAMA_OPENSSL=OFF \
    -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF \
    -DLLAMA_BUILD_UI=OFF -DLLAMA_USE_PREBUILT_UI=OFF \
    && cmake --build build --target llama-server -j 1

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /src/build/bin/llama-server /usr/local/bin/llama-server
COPY --from=build /src/LICENSE /usr/share/doc/llama.cpp/LICENSE
USER 65534:65534
ENTRYPOINT ["/usr/local/bin/llama-server"]
