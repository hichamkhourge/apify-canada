# Apify base image with Python + Google Chrome preinstalled (+ Xvfb). Chrome is
# driven by the Node/Puppeteer worker in browser/, not Selenium.
# https://hub.docker.com/r/apify/actor-python-selenium
FROM apify/actor-python-selenium:3.12

# libcairo2 is the native backend cairosvg needs to rasterize the IBO Player
# login captcha; nodejs/npm run the Puppeteer checkout worker (browser/). The
# Debian-based Apify image ships neither, and it runs as a non-root user, so
# switch to root for the apt install.
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends libcairo2 nodejs npm \
 && rm -rf /var/lib/apt/lists/*
# Restore the image's non-root user for the dependency install and runtime.
USER myuser

# Install Python dependencies first to leverage Docker layer caching.
COPY requirements.txt ./
RUN echo "Python version:" \
 && python --version \
 && echo "Installing dependencies:" \
 && pip install --no-cache-dir -r requirements.txt \
 && echo "Installed packages:" \
 && pip freeze

# Install the Puppeteer worker's Node dependencies. puppeteer-core uses the
# image's Google Chrome, so no browser download is needed.
COPY --chown=myuser browser/package.json browser/package-lock.json ./browser/
RUN cd browser \
 && PUPPETEER_SKIP_DOWNLOAD=true npm ci --omit=dev \
 && node --version

# Copy the rest of the source (actor wrapper + automation module + worker).
COPY . ./

# HEADLESS is intentionally False: the actor runs Chrome headed under an Xvfb
# virtual display, which is less bot-detectable by Cloudflare than headless.
ENV HEADLESS=False \
    AUTO_EXIT=True \
    IPTVV_DEBUG_DIR=/tmp/iptvv-logs

# Run the Apify Actor entry point.
CMD ["python3", "-m", "src"]
