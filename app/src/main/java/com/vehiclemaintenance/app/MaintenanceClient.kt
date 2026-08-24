package com.vehiclemaintenance.app

import android.util.Base64
import java.net.HttpURLConnection
import java.net.URL

/**
 * One item pulled off the server's dashboard: a single overdue or
 * due-soon service pill, e.g. "Brake Fluid Flush — 11,721 mi over".
 */
data class MaintenanceItem(
    val vehicleName: String,
    val status: String, // "overdue" or "due_soon"
    val label: String,
    val detail: String
)

/**
 * The server (see MainActivity's WebView) renders the whole dashboard as
 * plain server-side HTML with no JSON API behind it — the pill status for
 * each service item only exists baked into that markup. Rather than stand
 * up a second API just for the widget, this scrapes the same dashboard page
 * the app itself already points at, the same way the WebView does.
 */
object MaintenanceClient {

    // Matches one <div class='vehicle-card'>...</div> block's start, so the
    // page can be sliced into per-vehicle chunks (see fetchDueItems below).
    private val cardStartRegex = Regex("<div class='vehicle-card'>")
    private val nameRegex = Regex("<h2><a[^>]*>([^<]+)</a>")
    private val pillRegex = Regex(
        "<div class='pill (overdue|due_soon)'><span class='led'></span>" +
            "<span class='lbl'>([^<]*)</span><span class='det'>([^<]*)</span></div>"
    )

    /**
     * Every currently overdue or due-soon item across all vehicles, overdue
     * first. Returns null if the server couldn't be reached at all (as
     * opposed to an empty list, which means it WAS reached and everything is
     * caught up).
     */
    fun fetchDueItems(): List<MaintenanceItem>? {
        val html = fetchHtml() ?: return null

        val cardStarts = cardStartRegex.findAll(html).map { it.range.first }.toList()
        val items = mutableListOf<MaintenanceItem>()
        cardStarts.forEachIndexed { i, start ->
            val end = cardStarts.getOrElse(i + 1) { html.length }
            val card = html.substring(start, end)
            val vehicleName = nameRegex.find(card)?.groupValues?.get(1) ?: "Vehicle"
            pillRegex.findAll(card).forEach { match ->
                items.add(MaintenanceItem(vehicleName, match.groupValues[1], match.groupValues[2], match.groupValues[3]))
            }
        }

        return items.sortedBy { if (it.status == "overdue") 0 else 1 }
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
