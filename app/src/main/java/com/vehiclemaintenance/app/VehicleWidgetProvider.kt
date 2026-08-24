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
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * The home-screen widget's "brain" — see SysMonWidgetProvider in the
 * sysmon-widget project for the fuller explanation of what an
 * AppWidgetProvider is. This one has no per-widget configuration (there's
 * only ever one server, baked into Config/BuildConfig — see MainActivity),
 * so every placed instance always shows the exact same thing: every overdue
 * or due-soon service item across all vehicles, or a "you're all set"
 * message if there aren't any.
 *
 * updatePeriodMillis is 0 (see vehicle_widget_info.xml) — the widget only
 * ever refreshes when tapped, via ACTION_REFRESH below.
 */
class VehicleWidgetProvider : AppWidgetProvider() {

    companion object {
        const val ACTION_REFRESH = "com.vehiclemaintenance.app.ACTION_REFRESH"
        private const val MAX_ITEMS = 8
        private val timeFormat = SimpleDateFormat("h:mm a", Locale.getDefault())

        private fun updateOneWidget(context: Context, manager: AppWidgetManager, id: Int) {
            val views = RemoteViews(context.packageName, R.layout.widget_vehicle)

            // Tapping anywhere on the widget re-fetches from the server —
            // this IS the refresh mechanism, there's no timer.
            val refreshIntent = Intent(context, VehicleWidgetProvider::class.java).setAction(ACTION_REFRESH)
            val pendingIntent = PendingIntent.getBroadcast(
                context, 0, refreshIntent,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )
            views.setOnClickPendingIntent(R.id.widgetRoot, pendingIntent)

            val items = MaintenanceClient.fetchDueItems()
            views.removeAllViews(R.id.itemsList)

            if (items.isNullOrEmpty()) {
                views.setViewVisibility(R.id.itemsList, View.GONE)
                views.setViewVisibility(R.id.allGoodText, View.VISIBLE)
                if (items == null) {
                    views.setTextViewText(R.id.allGoodText, context.getString(R.string.widget_unreachable))
                    views.setTextColor(R.id.allGoodText, ContextCompat.getColor(context, R.color.text_dim))
                } else {
                    views.setTextViewText(R.id.allGoodText, context.getString(R.string.widget_all_good))
                    views.setTextColor(R.id.allGoodText, ContextCompat.getColor(context, R.color.ok))
                }
            } else {
                views.setViewVisibility(R.id.allGoodText, View.GONE)
                views.setViewVisibility(R.id.itemsList, View.VISIBLE)

                // Only bother naming the vehicle on each row once there's more
                // than one to tell apart — today that's always false (one
                // Forester), but the server already supports adding more.
                val showVehicleName = items.map { it.vehicleName }.distinct().size > 1

                items.take(MAX_ITEMS).forEach { item ->
                    val row = RemoteViews(context.packageName, R.layout.widget_item_row)
                    val dotRes = if (item.status == "overdue") R.drawable.widget_led_overdue else R.drawable.widget_led_due_soon
                    row.setImageViewResource(R.id.ledDot, dotRes)
                    row.setTextViewText(R.id.itemLabel, if (showVehicleName) "${item.vehicleName} — ${item.label}" else item.label)
                    row.setTextViewText(R.id.itemDetail, item.detail)
                    views.addView(R.id.itemsList, row)
                }
            }

            views.setTextViewText(R.id.updatedText, "Updated ${timeFormat.format(Date())}")

            manager.updateAppWidget(id, views)
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
}
