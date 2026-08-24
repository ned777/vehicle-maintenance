package com.vehiclemaintenance.app

import android.util.Base64
import java.net.HttpURLConnection
import java.net.URL

/** One vehicle on the dashboard, as offered by the widget's vehicle picker. */
data class Vehicle(val id: String, val name: String)

/** One service line from a vehicle's page, whatever its current status. */
data class MaintenanceItem(
    val status: String, // "overdue", "due_soon", "ok", or "unknown"
    val label: String,
    val detail: String // e.g. "4,071 mi left · due 2027-02-01"
)

/**
 * The server (see MainActivity's WebView) renders everything as plain
 * server-side HTML with no JSON API behind it — vehicle names and per-item
 * status only exist baked into that markup. Rather than stand up a second
 * API just for the widget, this scrapes the same pages the app's WebView
 * already points at.
 */
object MaintenanceClient {

    private val vehicleHeaderRegex = Regex("<h2><a href='/vehicle/(\\d+)'>([^<]+)</a></h2>")
    private val pillRegex = Regex(
        "<div class='pill (overdue|due_soon|ok|unknown)'><span class='led'></span>" +
            "<span class='lbl'>([^<]*)</span><span class='det'>([^<]*)</span></div>"
    )

    /** Every vehicle on the dashboard, in the order they're listed. */
    fun fetchVehicles(): List<Vehicle>? {
        val html = fetchHtml(Config.BASE_URL) ?: return null
        return vehicleHeaderRegex.findAll(html).map { Vehicle(it.groupValues[1], it.groupValues[2]) }.toList()
    }

    /**
     * Every tracked service item for ONE vehicle — overdue, due-soon, AND
     * fully-caught-up ("ok") ones, since the widget needs both (alerts when
     * there are any, otherwise a few caught-up highlights with mileage
     * remaining). Pulled from that vehicle's own page rather than the
     * dashboard index, which only ever lists its overdue/due-soon alerts.
     * Returns null if the server couldn't be reached at all.
     */
    fun fetchVehicleItems(vehicleId: String): List<MaintenanceItem>? {
        val html = fetchHtml("${Config.BASE_URL}/vehicle/$vehicleId") ?: return null
        return pillRegex.findAll(html)
            .map { MaintenanceItem(it.groupValues[1], it.groupValues[2], it.groupValues[3]) }
            .toList()
    }

    private fun fetchHtml(url: String): String? {
        return try {
            val connection = URL(url).openConnection() as HttpURLConnection
            connection.connectTimeout = 5000
            connection.readTimeout = 5000

            // The server basic-auth-protects every page (see MainActivity's
            // onReceivedHttpAuthRequest) — a plain HttpURLConnection has no
            // WebView to auto-answer that prompt, so the header is built by
            // hand here instead.
            val credentials = "${Config.USERNAME}:${Config.PASSWORD}"
            val encoded = Base64.encodeToString(credentials.toByteArray(), Base64.NO_WRAP)
            connection.setRequestProperty("Authorization", "Basic $encoded")

            try {
                if (connection.responseCode != 200) return null
                connection.inputStream.bufferedReader().readText()
            } finally {
                connection.disconnect()
            }
        } catch (e: Exception) {
            null
        }
    }
}
