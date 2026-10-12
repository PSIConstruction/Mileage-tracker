package com.psiconstruction.mileage

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.text.InputType
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.core.content.FileProvider
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Prototype UI: start/end a trip, enter odometer readings, watch live GPS
 * status, and share recorded trips (JSON) for processing.
 */
class MainActivity : Activity() {
    private lateinit var store: TripStore
    private lateinit var status: TextView
    private lateinit var live: TextView
    private lateinit var employee: EditText
    private lateinit var vehicle: EditText
    private lateinit var odometerStart: EditText
    private lateinit var startButton: Button
    private lateinit var stopButton: Button
    private lateinit var tripList: LinearLayout

    private val handler = Handler(Looper.getMainLooper())
    private val tick = object : Runnable {
        override fun run() {
            refreshLive()
            handler.postDelayed(this, 2000)
        }
    }
    private val prefs by lazy { getSharedPreferences("ui", MODE_PRIVATE) }
    private val timeFmt = SimpleDateFormat("EEE MMM d, h:mm a", Locale.US)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        store = TripStore(this)
        status = findViewById(R.id.status)
        live = findViewById(R.id.live)
        employee = findViewById(R.id.employee)
        vehicle = findViewById(R.id.vehicle)
        odometerStart = findViewById(R.id.odometer_start)
        startButton = findViewById(R.id.start)
        stopButton = findViewById(R.id.stop)
        tripList = findViewById(R.id.trips)

        employee.setText(prefs.getString("employee", ""))
        vehicle.setText(prefs.getString("vehicle", ""))

        startButton.setOnClickListener { onStartPressed() }
        stopButton.setOnClickListener { onStopPressed() }
        findViewById<Button>(R.id.share_all).setOnClickListener { shareTrips(store.tripIds()) }
        findViewById<Button>(R.id.battery).setOnClickListener {
            startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
        }
    }

    override fun onResume() {
        super.onResume()
        // If Android killed the service mid-trip, bring it back.
        if (store.activeTripId != null && hasPreciseLocation()) TrackingService.start(this)
        refreshAll()
        handler.post(tick)
    }

    override fun onPause() {
        handler.removeCallbacks(tick)
        super.onPause()
    }

    private fun onStartPressed() {
        if (!hasPreciseLocation()) {
            val perms = mutableListOf(
                Manifest.permission.ACCESS_FINE_LOCATION,
                Manifest.permission.ACCESS_COARSE_LOCATION,
            )
            if (Build.VERSION.SDK_INT >= 33) perms += Manifest.permission.POST_NOTIFICATIONS
            requestPermissions(perms.toTypedArray(), REQ_PERMS)
            return
        }
        prefs.edit()
            .putString("employee", employee.text.toString())
            .putString("vehicle", vehicle.text.toString())
            .apply()
        store.startTrip(
            employee.text.toString(),
            vehicle.text.toString(),
            odometerStart.text.toString().toDoubleOrNull(),
        )
        TrackingService.start(this)
        refreshAll()
    }

    private fun onStopPressed() {
        val id = store.activeTripId ?: return
        val odo = EditText(this).apply {
            hint = "Odometer at end"
            inputType = InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_FLAG_DECIMAL
        }
        val notes = EditText(this).apply { hint = "Notes (e.g. Shop → Newman)" }
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(48, 16, 48, 0)
            addView(odo)
            addView(notes)
        }
        AlertDialog.Builder(this)
            .setTitle("End trip")
            .setView(box)
            .setPositiveButton("End trip") { _, _ ->
                TrackingService.stop(this)
                store.endTrip(id, odo.text.toString().toDoubleOrNull(), notes.text.toString())
                odometerStart.setText(odo.text) // next trip usually starts here
                refreshAll()
            }
            .setNegativeButton("Keep recording", null)
            .show()
    }

    override fun onRequestPermissionsResult(code: Int, perms: Array<out String>, results: IntArray) {
        super.onRequestPermissionsResult(code, perms, results)
        if (code != REQ_PERMS) return
        if (hasPreciseLocation()) {
            onStartPressed()
        } else {
            AlertDialog.Builder(this)
                .setTitle("Precise location needed")
                .setMessage("Mileage needs Precise location. Open app settings → Permissions → " +
                    "Location → Allow while using the app, with “Use precise location” on.")
                .setPositiveButton("Open settings") { _, _ ->
                    startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                        Uri.fromParts("package", packageName, null)))
                }
                .setNegativeButton("Cancel", null)
                .show()
        }
    }

    private fun hasPreciseLocation() =
        checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED

    private fun refreshAll() {
        val active = store.activeTripId != null
        status.text = if (active) "● Recording trip" else "Not recording"
        startButton.isEnabled = !active
        stopButton.isEnabled = active
        odometerStart.isEnabled = !active
        refreshLive()
        refreshTrips()
    }

    private fun refreshLive() {
        val id = store.activeTripId
        val s = id?.let { store.summary(it) }
        if (s == null) {
            live.visibility = View.GONE
            return
        }
        live.visibility = View.VISIBLE
        val mins = (System.currentTimeMillis() - s.startedAt) / 60000
        live.text = buildString {
            appendLine("Elapsed:      $mins min")
            appendLine("GPS fixes:    ${s.pointCount}")
            appendLine("Last acc.:    ${s.lastAccuracy?.let { "%.0f m".format(it) } ?: "–"}")
            append("Raw distance: ${"%.2f".format(s.rawMeters / METERS_PER_MILE)} mi (unprocessed)")
        }
    }

    private fun refreshTrips() {
        tripList.removeAllViews()
        for (id in store.tripIds().take(50)) {
            val s = store.summary(id) ?: continue
            val row = LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL
                setPadding(0, 12, 0, 12)
            }
            val odo = if (s.odometerStart != null && s.odometerEnd != null)
                " · odometer %.1f mi".format(s.odometerEnd - s.odometerStart) else ""
            val label = TextView(this).apply {
                text = buildString {
                    append(timeFmt.format(Date(s.startedAt)))
                    if (s.endedAt == null) append(" (recording)")
                    append("\n${s.pointCount} fixes · raw %.2f mi".format(s.rawMeters / METERS_PER_MILE))
                    append(odo)
                    s.notes?.let { append("\n$it") }
                }
                layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
            }
            val share = Button(this).apply {
                setText("Share")
                setOnClickListener { shareTrips(listOf(id)) }
            }
            row.addView(label)
            row.addView(share)
            tripList.addView(row)
        }
    }

    private fun shareTrips(ids: List<String>) {
        if (ids.isEmpty()) {
            Toast.makeText(this, "No trips yet", Toast.LENGTH_SHORT).show()
            return
        }
        val uris = ArrayList(ids.map {
            FileProvider.getUriForFile(this, "$packageName.files", store.export(it))
        })
        val intent = if (uris.size == 1) {
            Intent(Intent.ACTION_SEND).putExtra(Intent.EXTRA_STREAM, uris[0])
        } else {
            Intent(Intent.ACTION_SEND_MULTIPLE).putParcelableArrayListExtra(Intent.EXTRA_STREAM, uris)
        }
        intent.type = "application/json"
        intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        startActivity(Intent.createChooser(intent, "Share trip data"))
    }

    companion object {
        private const val REQ_PERMS = 1
        private const val METERS_PER_MILE = 1609.344
    }
}
