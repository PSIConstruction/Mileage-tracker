package com.psiconstruction.mileage

import android.content.Context
import android.location.Location
import android.os.Build
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.UUID

/**
 * Append-only local storage for trips.
 *
 * Each trip is two files in the app's private storage:
 *  - `<id>.meta.json`    trip metadata (times, odometer, employee, vehicle, notes)
 *  - `<id>.points.jsonl` one raw GPS fix per line, appended and flushed as it
 *                        arrives so a crash or a killed app loses at most one fix.
 *
 * Raw fixes are never edited. Export assembles them into the
 * `psi-mileage-trip/1` JSON format the processing pipeline reads.
 */
class TripStore(context: Context) {
    private val dir = File(context.filesDir, "trips").apply { mkdirs() }
    private val exportDir = File(context.cacheDir, "export")
    private val prefs = context.getSharedPreferences("trip_store", Context.MODE_PRIVATE)

    data class Summary(
        val id: String,
        val startedAt: Long,
        val endedAt: Long?,
        val pointCount: Int,
        val rawMeters: Double,
        val lastAccuracy: Float?,
        val odometerStart: Double?,
        val odometerEnd: Double?,
        val notes: String?,
    )

    var activeTripId: String?
        get() = prefs.getString(KEY_ACTIVE, null)
        private set(value) = prefs.edit().putString(KEY_ACTIVE, value).apply()

    @Synchronized
    fun startTrip(employee: String?, vehicle: String?, odometerStart: Double?): String {
        val now = System.currentTimeMillis()
        val stamp = SimpleDateFormat("yyyy-MM-dd'T'HH-mm-ss", Locale.US).format(Date(now))
        val id = "${stamp}_${UUID.randomUUID().toString().take(4)}"
        val meta = JSONObject()
            .put("tripId", id)
            .put("startedAt", now)
            .putOpt("employee", employee?.ifBlank { null })
            .putOpt("vehicle", vehicle?.ifBlank { null })
            .putOpt("odometerStart", odometerStart)
            .put("device", "${Build.MANUFACTURER} ${Build.MODEL} (Android ${Build.VERSION.RELEASE})")
            .put("samplingIntervalMs", TrackingService.INTERVAL_MS)
        writeMeta(id, meta)
        pointsFile(id).createNewFile()
        activeTripId = id
        return id
    }

    @Synchronized
    fun endTrip(id: String, odometerEnd: Double?, notes: String?) {
        val meta = readMeta(id) ?: return
        meta.put("endedAt", System.currentTimeMillis())
            .putOpt("odometerEnd", odometerEnd)
            .putOpt("notes", notes?.ifBlank { null })
        writeMeta(id, meta)
        if (activeTripId == id) activeTripId = null
    }

    @Synchronized
    fun appendPoint(id: String, l: Location) {
        val p = JSONObject()
            .put("t", l.time)
            .put("lat", l.latitude)
            .put("lng", l.longitude)
            .put("elapsedNs", l.elapsedRealtimeNanos)
        if (l.hasAccuracy()) p.put("acc", l.accuracy.toDouble())
        if (l.hasSpeed()) p.put("speed", l.speed.toDouble())
        if (l.hasSpeedAccuracy()) p.put("speedAcc", l.speedAccuracyMetersPerSecond.toDouble())
        if (l.hasBearing()) p.put("bearing", l.bearing.toDouble())
        if (l.hasAltitude()) p.put("alt", l.altitude)
        if (l.hasVerticalAccuracy()) p.put("altAcc", l.verticalAccuracyMeters.toDouble())
        p.putOpt("provider", l.provider)
        val mock = if (Build.VERSION.SDK_INT >= 31) l.isMock else @Suppress("DEPRECATION") l.isFromMockProvider
        if (mock) p.put("mock", true)
        FileOutputStream(pointsFile(id), true).use { out ->
            out.write((p.toString() + "\n").toByteArray())
            out.fd.sync()
        }
    }

    @Synchronized
    fun summary(id: String): Summary? {
        val meta = readMeta(id) ?: return null
        var count = 0
        var meters = 0.0
        var prev: JSONObject? = null
        var lastAcc: Float? = null
        val result = FloatArray(1)
        pointsFile(id).takeIf { it.exists() }?.forEachLine { line ->
            if (line.isBlank()) return@forEachLine
            val p = try { JSONObject(line) } catch (e: Exception) { return@forEachLine }
            prev?.let {
                Location.distanceBetween(it.getDouble("lat"), it.getDouble("lng"),
                    p.getDouble("lat"), p.getDouble("lng"), result)
                meters += result[0]
            }
            lastAcc = if (p.has("acc")) p.getDouble("acc").toFloat() else null
            prev = p
            count++
        }
        return Summary(
            id = id,
            startedAt = meta.getLong("startedAt"),
            endedAt = meta.optLongOrNull("endedAt"),
            pointCount = count,
            rawMeters = meters,
            lastAccuracy = lastAcc,
            odometerStart = meta.optDoubleOrNull("odometerStart"),
            odometerEnd = meta.optDoubleOrNull("odometerEnd"),
            notes = meta.optString("notes").ifBlank { null },
        )
    }

    fun tripIds(): List<String> =
        dir.listFiles { f -> f.name.endsWith(META_SUFFIX) }
            ?.map { it.name.removeSuffix(META_SUFFIX) }
            ?.sortedDescending()
            ?: emptyList()

    /** Write the trip as a single `psi-mileage-trip/1` JSON file for sharing. */
    @Synchronized
    fun export(id: String): File {
        val meta = readMeta(id) ?: error("unknown trip $id")
        val points = JSONArray()
        pointsFile(id).forEachLine { line ->
            if (line.isNotBlank()) {
                try { points.put(JSONObject(line)) } catch (_: Exception) { /* torn last line */ }
            }
        }
        val out = JSONObject(meta.toString())
            .put("format", "psi-mileage-trip/1")
            .put("points", points)
        exportDir.mkdirs()
        return File(exportDir, "$id.json").apply { writeText(out.toString()) }
    }

    private fun pointsFile(id: String) = File(dir, "$id.points.jsonl")
    private fun metaFile(id: String) = File(dir, "$id$META_SUFFIX")

    private fun readMeta(id: String): JSONObject? =
        metaFile(id).takeIf { it.exists() }?.let { JSONObject(it.readText()) }

    private fun writeMeta(id: String, meta: JSONObject) {
        // Write-then-rename so a crash never leaves a half-written file.
        val tmp = File(dir, "$id.meta.tmp")
        tmp.writeText(meta.toString(1))
        tmp.renameTo(metaFile(id))
    }

    private fun JSONObject.optLongOrNull(key: String) = if (has(key)) getLong(key) else null
    private fun JSONObject.optDoubleOrNull(key: String) = if (has(key)) getDouble(key) else null

    companion object {
        private const val KEY_ACTIVE = "active_trip"
        private const val META_SUFFIX = ".meta.json"
    }
}
