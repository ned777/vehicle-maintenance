package com.vehiclemaintenance.app

import android.util.Base64
import java.net.HttpURLConnection
import java.net.URL

/** One vehicle on the dashboard, as offered by the widget's vehicle picker. */
data class Vehicle(val id: String, val name: String)

/** One overdue or due-soon service pill for a specific vehicle. */
data class MaintenanceItem(
    val vehicleId: String,
    val status: String, // "overdue" or "due_soon"
    val label: String
)

/**
 * The server (see MainActivity's WebView) renders the whole dashboard as
 * plain server-side HTML with no JSON API behind it — vehicle names and
 * pill status only exist baked into that markup. Rather than stand up a
 * second API just for the widget, this scrapes the same dashboard page the
 * app itself already points at, the same way the WebView does.
 */
object MaintenanceClient {

    private val vehicleHeaderRegex = Regex("<h2><a href='/vehicle/(\\d+)'>([^<]+)</a></h2>")
    private val cardStartRegex = Regex("<div class='vehicle-card'>")
    private val pillRegex = Regex(
        "<div class='pill (overdue|due_soon)'><span class='led'></span>" +
            "<span class='lbl'>([^<]*)</span><span class='det'>[^<]*</span></div>"
    )

    /** Every vehicle on the dashboard, in the order they're listed. */
    fun fetchVehicles(): List<Vehicle>? {
        val html = fetchHtml() ?: return null
        return vehicleHeaderRegex.findAll(html).map { Vehicle(it.groupValues[1], it.groupValues[2]) }.toList()
    }

    /**
     * Every currently overdue or due-soon item for ONE vehicle, overdue
     * first. Returns null if the server couldn't be reached at all (as
     * opposed to an empty list, which means it WAS reached and this vehicle
     * is fully caught up).
     */
    fun fetchDueItems(vehicleId: String): List<MaintenanceItem>? {
        val html = fetchHtml() ?: return null
        val card = cardHtmlFor(html, vehicleId) ?: return emptyList()
        return pillRegex.findAll(card)
            .map { MaintenanceItem(vehicleId, it.groupValues[1], it.groupValues[2]) }
            .sortedBy { if (it.status == "overdue") 0 else 1 }
            .toList()
    }

    // Slices the page down to just the one <div class='vehicle-card'>...</div>
    // block for vehicleId, so pillRegex only ever sees that vehicle's own
    // pills and never another car's.
    private fun cardHtmlFor(html: String, vehicleId: String): String? {
        val starts = cardStartRegex.findAll(html).map { it.range.first }.toList()
        starts.forEachIndexed { i, start ->
            val end = starts.getOrElse(i + 1) { html.length }
            val card = html.substring(start, end)
            val id = vehicleHeaderRegex.find(card)?.groupValues?.get(1)
            if (id == vehicleId) return card
        }
        return null
    }

    private fun fetchHtml(): String? {
        return try {
            val connection = URL(Config.BASE_URL).openConnection() as HttpURLConnection
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
