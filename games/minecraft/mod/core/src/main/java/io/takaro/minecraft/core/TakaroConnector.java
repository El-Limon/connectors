package io.takaro.minecraft.core;

import io.takaro.minecraft.core.target.RuntimeIdentity;
import io.takaro.minecraft.core.target.TargetGuard;
import io.takaro.minecraft.core.target.TargetInfo;

import java.net.URI;
import java.util.Optional;

public class TakaroConnector {

    private final GameAdapter adapter;
    private final TakaroConfig config;
    private final Optional<TargetInfo> targetInfo;
    private TakaroWebSocketClient wsClient;

    public TakaroConnector(GameAdapter adapter, TakaroConfig config) {
        this(adapter, config, TargetInfo.load());
    }

    public TakaroConnector(GameAdapter adapter, TakaroConfig config, Optional<TargetInfo> targetInfo) {
        this.adapter = adapter;
        this.config = config;
        this.targetInfo = targetInfo == null ? Optional.empty() : targetInfo;
    }

    public void connect() {
        String url = config.getWsUrl();
        if (url == null || url.isEmpty()) {
            adapter.logWarning("No WebSocket URL configured, cannot connect");
            return;
        }

        // Check the build target before opening a socket: a mod built for another game
        // version half-works, and half-working looks like a Takaro outage.
        RuntimeIdentity identity = adapter.getRuntimeIdentity();
        TargetGuard.Policy policy = TargetGuard.Policy.parse(config.getTargetPolicy());
        TargetGuard.Decision decision = TargetGuard.evaluate(targetInfo, identity, policy);
        adapter.logInfo("Takaro target-check: "
                + TargetGuard.toJson(targetInfo.orElse(null), identity, decision));
        if (!decision.connect()) {
            String id = targetInfo.map(TargetInfo::target).orElse("unknown");
            adapter.logWarning("Takaro refuses to connect: target " + id
                    + " does not match this server (" + String.join("; ", decision.reasons())
                    + "). Set TAKARO_TARGET_POLICY=warn to override.");
            return;
        }

        if (wsClient != null) {
            wsClient.shutdown();
        }

        adapter.logInfo("Connecting to Takaro at " + url);
        if (config.isDebugEnabled()) {
            adapter.logDebug("Config: wsUrl=" + url + ", reconnect=" + config.isReconnectEnabled()
                    + ", reconnectDelay=" + config.getReconnectDelay() + ", debug=true");
        }
        try {
            wsClient = new TakaroWebSocketClient(new URI(url), adapter, config);
            adapter.setEventEmitter(wsClient);
            wsClient.connect();
        } catch (Exception e) {
            adapter.logWarning("Failed to create WebSocket connection: " + e.getMessage());
        }
    }

    public void shutdown() {
        if (wsClient != null) {
            adapter.logInfo("Shutting down Takaro connection");
            wsClient.shutdown();
        }
    }
}
