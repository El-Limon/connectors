package io.takaro.minecraft.core.target;

import com.google.gson.JsonObject;
import com.google.gson.JsonParser;

import java.io.InputStream;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.util.Optional;

/**
 * The build target this jar was stamped with, read back from its own classpath.
 *
 * The file is written by the Gradle target conventions from the same catalog record the
 * maintenance command resolves, so a running server can tell exactly what it loaded.
 */
public final class TargetInfo {

    public static final String RESOURCE = "/META-INF/takaro-target.json";

    private final String target;
    private final String fingerprint;
    private final String game;
    private final String platform;
    private final String revision;
    private final String connectorVersion;
    private final String sourceRevision;
    private final String gameVersion;
    private final String loaderMin;
    private final int javaMin;

    private TargetInfo(String target, String fingerprint, String game, String platform, String revision,
                       String connectorVersion, String sourceRevision, String gameVersion, String loaderMin,
                       int javaMin) {
        this.target = target;
        this.fingerprint = fingerprint;
        this.game = game;
        this.platform = platform;
        this.revision = revision;
        this.connectorVersion = connectorVersion;
        this.sourceRevision = sourceRevision;
        this.gameVersion = gameVersion;
        this.loaderMin = loaderMin;
        this.javaMin = javaMin;
    }

    public static TargetInfo of(String target, String fingerprint, String game, String platform, String revision,
                                String connectorVersion, String sourceRevision, String gameVersion, String loaderMin,
                                int javaMin) {
        return new TargetInfo(target, fingerprint, game, platform, revision, connectorVersion, sourceRevision,
                gameVersion, loaderMin, javaMin);
    }

    /** Empty when the jar carries no stamp — an unstamped build is reported, never guessed at. */
    public static Optional<TargetInfo> load() {
        return load(TargetInfo.class);
    }

    static Optional<TargetInfo> load(Class<?> anchor) {
        try (InputStream stream = anchor.getResourceAsStream(RESOURCE)) {
            if (stream == null) {
                return Optional.empty();
            }
            JsonObject root = JsonParser
                    .parseReader(new InputStreamReader(stream, StandardCharsets.UTF_8))
                    .getAsJsonObject();
            JsonObject constraints = root.has("constraints") ? root.getAsJsonObject("constraints") : new JsonObject();
            return Optional.of(new TargetInfo(
                    string(root, "target"),
                    string(root, "fingerprint"),
                    string(root, "game"),
                    string(root, "platform"),
                    string(root, "revision"),
                    string(root, "connectorVersion"),
                    string(root, "sourceRevision"),
                    string(constraints, "gameVersion"),
                    string(constraints, "loaderMin"),
                    constraints.has("javaMin") ? constraints.get("javaMin").getAsInt() : 0));
        } catch (Exception e) {
            return Optional.empty();
        }
    }

    private static String string(JsonObject object, String key) {
        return object.has(key) && !object.get(key).isJsonNull() ? object.get(key).getAsString() : "";
    }

    public String target() { return target; }
    public String fingerprint() { return fingerprint; }
    public String game() { return game; }
    public String platform() { return platform; }
    public String revision() { return revision; }
    public String connectorVersion() { return connectorVersion; }
    public String sourceRevision() { return sourceRevision; }
    public String gameVersion() { return gameVersion; }
    public String loaderMin() { return loaderMin; }
    public int javaMin() { return javaMin; }

    @Override
    public String toString() {
        return "TargetInfo{" + target + "@" + fingerprint + "}";
    }
}
