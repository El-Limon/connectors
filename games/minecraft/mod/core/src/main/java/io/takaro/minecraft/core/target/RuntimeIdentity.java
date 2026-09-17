package io.takaro.minecraft.core.target;

/**
 * What the server actually is, as the running platform reports it.
 *
 * @param gameVersion   the game version the server is running, e.g. "26.2"
 * @param loader        the mod loader, e.g. "fabric"
 * @param loaderVersion the loader's own version, e.g. "0.19.5"
 * @param javaMajor     the JVM feature release, e.g. 25
 */
public record RuntimeIdentity(String gameVersion, String loader, String loaderVersion, int javaMajor) {
}
