from django.contrib.gis.db import models
from django.contrib.gis.db.models.functions import Distance as GeoDistance


class FuelStation(models.Model):
    opis_id  = models.CharField(max_length=20, unique=True)
    name     = models.CharField(max_length=255)
    address  = models.CharField(max_length=255)
    city     = models.CharField(max_length=100)
    state    = models.CharField(max_length=2)
    rack_id  = models.CharField(max_length=20, blank=True)
    price    = models.DecimalField(max_digits=8, decimal_places=5)
    location = models.PointField(srid=4326)

    class Meta:
        indexes = [
            models.Index(fields=['state']),
            models.Index(fields=['price']),
        ]

    def __str__(self):
        return f"{self.name} ({self.state}) — ${self.price}"


class RouteDistance(models.Model):
    """
    Caches the computed road distance between two geographic points.
    Keyed by a pair of snapped coordinate strings so repeated
    corridor queries (same sample mile, different trips) get a free hit.
    """
    origin_lat      = models.DecimalField(max_digits=9, decimal_places=6)
    origin_lng      = models.DecimalField(max_digits=9, decimal_places=6)
    dest_lat        = models.DecimalField(max_digits=9, decimal_places=6)
    dest_lng        = models.DecimalField(max_digits=9, decimal_places=6)

    haversine_miles = models.FloatField()
    road_miles      = models.FloatField()   # blended road-factor estimate
    road_factor     = models.FloatField()   # actual factor used  (road / haversine)

    created_at      = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('origin_lat', 'origin_lng', 'dest_lat', 'dest_lng')]
        indexes = [
            models.Index(fields=['origin_lat', 'origin_lng']),
        ]

    def __str__(self):
        return (
            f"({self.origin_lat},{self.origin_lng}) → "
            f"({self.dest_lat},{self.dest_lng}) "
            f"{self.road_miles:.1f} mi"
        )