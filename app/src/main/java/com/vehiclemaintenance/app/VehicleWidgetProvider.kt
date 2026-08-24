package com.vehiclemaintenance.app

import android.app.PendingIntent
import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProvider
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.view.View
import android.widget.RemoteViews
import androidx.core.content.ContextCompat

/**
 * The home-screen widget's "brain" — see SysMonWidgetProvider in the
 * sysmon-widget project for the fuller explanation of what an
 * AppWidgetProvider is. Each placed instance is configured to watch ONE
 * vehicle (picked in WidgetConfigActivity, since the server can hold more
 * than one car), and always shows the same four categories — Oil,
 * Transmission, Differential, Spark Plugs — each with its mileage
 * remaining. The status dot still reflects that item's real state
 * (red/amber/green) in case one of these four happens to be overdue or due
 * soon.
 *
 * updatePeriodMillis is 0 (see vehicle_widget_info.xml) — the widget only
 * ever refreshes when tapped, via ACTION_REFRESH below.
 */
class VehicleWidgetProvider : AppWidgetProvider() {

    companion object {
        const val ACTION_REFRESH = "com.vehiclemaintenance.app.ACTION_REFRESH"
        const val PREFS_NAME = "vehicle_widget"

        // The four categories this widget always shows, each matched by
        // keyword against whatever the server actually labeled them (e.g.
        // this Subaru's CVT fluid is labeled "Transmission Fluid") but
        // displayed under our own short name rather than the server's.
        private val FIXED_CATEGORIES = listOf(
            "Oil" to listOf("oil"),
            "Transmission" to listOf("cvt", "transmission"),
            "Differential" to listOf("diff"),
            "Spark Plugs" to listOf("spark")
        )

        fun refreshAllWidgets(context: Context) {
            val intent = Intent(context, VehicleWidgetProvider::class.java).setAction(ACTION_REFRESH)
            context.sendBroadcast(intent)
        }

        fun updateOneWidget(context: Context, manager: AppWidgetManager, id: Int) {
            val views = RemoteViews(context.packageName, R.layout.widget_vehicle)

            // Tapping anywhere on the widget re-fetches from the server —
            // this IS the refresh mechanism, there's no timer.
            val refreshIntent = Intent(context, VehicleWidgetProvider::class.java).setAction(ACTION_REFRESH)
            val refreshPendingIntent = PendingIntent.getBroadcast(
                context, 0, refreshIntent,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )
            views.setOnClickPendingIntent(R.id.widgetRoot, refreshPendingIntent)

            views.removeAllViews(R.id.itemsList)

            val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
            val vehicleId = prefs.getString("widget_${id}_vehicle_id", null)
            val vehicleName = prefs.getString("widget_${id}_vehicle_name", null)

            if (vehicleId == null) {
                // Can happen if the configured vehicle's prefs were somehow
                // cleared — normally every placed widget has one, since
                // WidgetConfigActivity is required before placement finishes.
                views.setTextViewText(R.id.vehicleNameText, context.getString(R.string.app_name))
                showMessage(context, views, context.getString(R.string.widget_no_vehicle), R.color.text_dim)
                manager.updateAppWidget(id, views)
                return
            }

            views.setTextViewText(R.id.vehicleNameText, vehicleName ?: context.getString(R.string.app_name))

            val items = MaintenanceClient.fetchVehicleItems(vehicleId)
            if (items == null) {
                showMessage(context, views, context.getString(R.string.widget_unreachable), R.color.text_dim)
                manager.updateAppWidget(id, views)
                return
            }

            val highlights = FIXED_CATEGORIES.mapNotNull { (displayName, keywords) ->
                val match = items.firstOrNull { item -> keywords.any { item.label.contains(it, ignoreCase = true) } }
                match?.let { displayName to it }
            }

            if (highlights.isEmpty()) {
                // None of the four usual categories are tracked for this
                // vehicle — fall back to a plain message rather than
                // showing an empty list.
                showMessage(context, views, context.getString(R.string.widget_all_good), R.color.ok)
            } else {
                views.setViewVisibility(R.id.allGoodText, View.GONE)
                views.setViewVisibility(R.id.itemsList, View.VISIBLE)
                highlights.forEach { (displayName, item) ->
                    views.addView(R.id.itemsList, buildHighlightRow(context, displayName, item))
                }
            }

            manager.updateAppWidget(id, views)
        }

        private fun showMessage(context: Context, views: RemoteViews, message: String, colorRes: Int) {
            views.setViewVisibility(R.id.itemsList, View.GONE)
            views.setViewVisibility(R.id.allGoodText, View.VISIBLE)
            views.setTextViewText(R.id.allGoodText, message)
            views.setTextColor(R.id.allGoodText, ContextCompat.getColor(context, colorRes))
        }

        // Our own short category name, plus just the mileage-remaining
        // clause of the item's detail string (e.g. "4,071 mi left · due
        // 2027-02-01" -> "4,071 mi left") — the due date isn't the point
        // here.
        private fun buildHighlightRow(context: Context, displayName: String, item: MaintenanceItem): RemoteViews {
            val row = RemoteViews(context.packageName, R.layout.widget_item_row)
            val dotRes = when (item.status) {
                "overdue" -> R.drawable.widget_led_overdue
                "due_soon" -> R.drawable.widget_led_due_soon
                else -> R.drawable.widget_led_ok
            }
            row.setImageViewResource(R.id.ledDot, dotRes)
            row.setTextViewText(R.id.itemLabel, displayName)
            row.setTextViewText(R.id.itemDetail, item.detail.substringBefore("·").trim())
            row.setViewVisibility(R.id.itemDetail, View.VISIBLE)
            return row
        }
    }

    override fun onUpdate(context: Context, appWidgetManager: AppWidgetManager, appWidgetIds: IntArray) {
        val pending = goAsync()
        Thread {
            try {
                appWidgetIds.forEach { updateOneWidget(context, appWidgetManager, it) }
            } finally {
                pending.finish()
            }
        }.start()
    }

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == ACTION_REFRESH) {
            val pending = goAsync()
            Thread {
                try {
                    val manager = AppWidgetManager.getInstance(context)
                    val ids = manager.getAppWidgetIds(ComponentName(context, VehicleWidgetProvider::class.java))
                    ids.forEach { updateOneWidget(context, manager, it) }
                } finally {
                    pending.finish()
                }
            }.start()
        } else {
            super.onReceive(context, intent)
        }
    }

    override fun onDeleted(context: Context, appWidgetIds: IntArray) {
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        val editor = prefs.edit()
        appWidgetIds.forEach { id ->
            editor.remove("widget_${id}_vehicle_id")
            editor.remove("widget_${id}_vehicle_name")
        }
        editor.apply()
    }
}
