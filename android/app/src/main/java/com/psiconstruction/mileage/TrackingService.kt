package com.psiconstruction.mileage

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.os.Looper
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import com.google.android.gms.location.FusedLocationProviderClient
import com.google.android.gms.location.LocationCallback
import com.google.android.gms.location.LocationRequest
import com.google.android.gms.location.LocationResult
import com.google.android.gms.location.LocationServices
import com.google.android.gms.location.Priority

/**
 * Foreground service that records a high-accuracy GPS fix every ~5 seconds
 * while a trip is active. Running as a `location` foreground service (with a
 * visible notification) is what keeps Android from throttling or killing
 * location updates when the screen is off.
 */
class TrackingService : Service() {
    private lateinit var store: TripStore
    private lateinit var fused: FusedLocationProviderClient
    private var tripId: String? = null
    private var fixes = 0

    private val callback = object : LocationCallback() {
        override fun onLocationResult(result: LocationResult) {
            val id = tripId ?: return
            for (loc in result.locations) {
                store.appendPoint(id, loc)
                fixes++
            }
            notify("$fixes GPS fixes recorded")
        }
    }

    override fun onCreate() {
        super.onCreate()
        store = TripStore(this)
        fused = LocationServices.getFusedLocationProviderClient(this)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                stopTracking()
                return START_NOT_STICKY
            }
            else -> {
                // ACTION_START, or a null intent when Android restarts the
                // service after killing it: resume the active trip if any.
                val id = store.activeTripId
                if (id == null) {
                    stopSelf()
                    return START_NOT_STICKY
                }
                startTracking(id)
            }
        }
        return START_STICKY
    }

    private fun startTracking(id: String) {
        try {
            ServiceCompat.startForeground(
                this, NOTIFICATION_ID, buildNotification("Waiting for GPS…"),
                if (Build.VERSION.SDK_INT >= 29) ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION else 0,
            )
        } catch (e: Exception) {
            // Android may refuse to restart a location service from the
            // background. The trip stays active; reopening the app resumes it.
            stopSelf()
            return
        }
        if (tripId == id) return
        tripId = id
        fixes = 0
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION)
            != PackageManager.PERMISSION_GRANTED
        ) {
            notify("Precise location permission missing — open the app")
            return
        }
        val request = LocationRequest.Builder(Priority.PRIORITY_HIGH_ACCURACY, INTERVAL_MS)
            .setMinUpdateIntervalMillis(INTERVAL_MS)
            .setMaxUpdateDelayMillis(0) // deliver immediately, no batching
            .setMinUpdateDistanceMeters(0f) // filtering happens on the server, never here
            .setWaitForAccurateLocation(true)
            .build()
        fused.requestLocationUpdates(request, callback, Looper.getMainLooper())
    }

    private fun stopTracking() {
        fused.removeLocationUpdates(callback)
        tripId = null
        ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    override fun onDestroy() {
        fused.removeLocationUpdates(callback)
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun notify(text: String) {
        getSystemService(NotificationManager::class.java)
            .notify(NOTIFICATION_ID, buildNotification(text))
    }

    private fun buildNotification(text: String): Notification {
        val nm = getSystemService(NotificationManager::class.java)
        if (nm.getNotificationChannel(CHANNEL_ID) == null) {
            nm.createNotificationChannel(
                NotificationChannel(CHANNEL_ID, getString(R.string.channel_name),
                    NotificationManager.IMPORTANCE_LOW)
            )
        }
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.ic_menu_mylocation)
            .setContentTitle(getString(R.string.notification_title))
            .setContentText(text)
            .setContentIntent(open)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .build()
    }

    companion object {
        const val INTERVAL_MS = 5_000L
        private const val CHANNEL_ID = "trip"
        private const val NOTIFICATION_ID = 1
        private const val ACTION_START = "com.psiconstruction.mileage.START"
        private const val ACTION_STOP = "com.psiconstruction.mileage.STOP"

        fun start(context: Context) {
            ContextCompat.startForegroundService(
                context, Intent(context, TrackingService::class.java).setAction(ACTION_START))
        }

        fun stop(context: Context) {
            context.startService(
                Intent(context, TrackingService::class.java).setAction(ACTION_STOP))
        }
    }
}
