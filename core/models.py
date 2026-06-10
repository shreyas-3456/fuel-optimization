from django.contrib.gis.db import models

class FuelStation(models.Model):
    opis_id     = models.CharField(max_length=20, unique=True)
    name        = models.CharField(max_length=255)
    address     = models.CharField(max_length=255)
    city        = models.CharField(max_length=100)
    state       = models.CharField(max_length=2)
    rack_id     = models.CharField(max_length=20, blank=True)
    price       = models.DecimalField(max_digits=8, decimal_places=5)
    location    = models.PointField(srid=4326)  # stores lat/lng as PostGIS geometry

    class Meta:
        indexes = [
            models.Index(fields=['state']),
            models.Index(fields=['price']),
        ]

    def __str__(self):
        return f"{self.name} ({self.state}) — ${self.price}"