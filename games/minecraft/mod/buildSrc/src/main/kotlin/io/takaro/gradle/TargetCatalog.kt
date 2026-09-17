package io.takaro.gradle

import groovy.json.JsonSlurper
import java.io.File
import java.security.MessageDigest
import org.gradle.api.Project

/**
 * The Gradle half of the catalog: it reads the same target JSON the `takaro-maint`
 * command reads, and computes the same fingerprint from it.
 *
 * The canonical serializer below is hand-written on purpose. `JsonOutput` would escape
 * differently from Python's, and the fingerprint has to agree byte for byte with
 * `maintenance/src/takaro_maint/fingerprint.py` — a parity fixture proves it does.
 */
object TargetCatalog {

    /** Keys the fingerprint covers; everything else (support notes, components) may change freely. */
    val FINGERPRINT_KEYS = listOf("id", "game", "platform", "revision", "inputs", "runtime", "build")

    data class Record(val data: Map<String, Any?>, val file: File) {
        val id: String get() = data["id"] as String
        val game: String get() = data["game"] as String
        val platform: String get() = data["platform"] as String
        val revision: String get() = data["revision"] as String

        @Suppress("UNCHECKED_CAST")
        val build: Map<String, Any?> get() = data["build"] as Map<String, Any?>

        @Suppress("UNCHECKED_CAST")
        val inputs: Map<String, Any?> get() = data["inputs"] as Map<String, Any?>

        @Suppress("UNCHECKED_CAST")
        val runtime: Map<String, Any?> get() = data["runtime"] as Map<String, Any?>

        @Suppress("UNCHECKED_CAST")
        val deps: Map<String, Map<String, Any?>> get() = build["deps"] as Map<String, Map<String, Any?>>

        val javaRelease: Int get() = (build["javaRelease"] as Number).toInt()
        val fingerprint: String get() = fingerprint(data)
        val fp16: String get() = fingerprint.substring(0, 16)

        fun coordinate(dep: String): String = deps.getValue(dep)["coordinate"] as String
        fun sha256(dep: String): String? = deps.getValue(dep)["sha256"] as String?

        @Suppress("UNCHECKED_CAST")
        fun input(name: String): Map<String, Any?> = inputs[name] as Map<String, Any?>
    }

    /** The repository root, found by walking up from the Gradle root project. */
    fun repoRoot(project: Project): File = project.rootDir.parentFile.parentFile.parentFile

    /** Load the record whose id matches the Gradle project's name. */
    fun load(project: Project): Record {
        val id = project.name.replace('_', '.')
        val root = repoRoot(project)
        val catalogDir = File(root, "catalog")
        val gameDirs = catalogDir.listFiles { file: File -> file.isDirectory }.orEmpty()
        for (gameDir in gameDirs.sortedBy { it.name }) {
            val file = File(gameDir, "targets/$id.json")
            if (!file.isFile) continue
            @Suppress("UNCHECKED_CAST")
            val data = JsonSlurper().parse(file) as Map<String, Any?>
            val declared = data["id"]
            require(declared == id) {
                "catalog record ${file.path} declares id '$declared' but the Gradle project is '$id'"
            }
            return Record(data, file)
        }
        throw IllegalStateException("no catalog target '$id' under ${catalogDir.path}")
    }

    /** JSON with keys sorted recursively, no whitespace, integers only, minimal escaping. */
    fun canonicalJson(value: Any?): String = when (value) {
        null -> "null"
        is Boolean -> value.toString()
        is String -> escape(value)
        is Int, is Long, is Short, is Byte -> value.toString()
        is java.math.BigInteger -> value.toString()
        is java.math.BigDecimal ->
            if (value.stripTrailingZeros().scale() <= 0) {
                value.toBigIntegerExact().toString()
            } else {
                throw IllegalArgumentException("the catalog forbids floats; fingerprints are integer-only")
            }
        is Number -> throw IllegalArgumentException("the catalog forbids floats; fingerprints are integer-only")
        is Map<*, *> -> value.entries
            .sortedBy { it.key.toString() }
            .joinToString(",", "{", "}") { escape(it.key.toString()) + ":" + canonicalJson(it.value) }
        is Iterable<*> -> value.joinToString(",", "[", "]") { canonicalJson(it) }
        else -> throw IllegalArgumentException("cannot canonicalise " + value::class.java.name)
    }

    private fun escape(text: String): String {
        val out = StringBuilder(text.length + 2)
        out.append('"')
        for (ch in text) {
            when (ch.code) {
                0x22 -> out.append("\\\"")
                0x5C -> out.append("\\\\")
                0x0A -> out.append("\\n")
                0x0D -> out.append("\\r")
                0x09 -> out.append("\\t")
                0x08 -> out.append("\\b")
                0x0C -> out.append("\\f")
                else -> if (ch.code < 0x20) out.append(String.format("\\u%04x", ch.code)) else out.append(ch)
            }
        }
        out.append('"')
        return out.toString()
    }

    fun fingerprint(record: Map<String, Any?>): String {
        val subset = LinkedHashMap<String, Any?>()
        for (key in FINGERPRINT_KEYS) if (record.containsKey(key)) subset[key] = record[key]
        val digest = MessageDigest.getInstance("SHA-256")
        val bytes = digest.digest(canonicalJson(subset).toByteArray(Charsets.UTF_8))
        return bytes.joinToString("") { String.format("%02x", it) }
    }
}
