# cars – samochody użytkownika

Modele: Cars (per user) → CarFuelConsumption (tankowania), CarService → CarServicePart, CarTyres → CarTyreUsage (okresy założenia opon).
Widoki (`views.py`):
- GarageView, AddCarView, CarDashboardView.
- CRUD tankowań, serwisów, opon i okresów użycia opon.
- ServiceHistoryPdfView – PDF historii serwisów (`pdf_utils.py`, reportlab).
Pomocnicze: `recalculate_fuel_consumptions` (spalanie z dystansów), aktualizacja przebiegu z serwisu i opon. Usuwanie przez `delete_with_undo` z przeliczeniem po cofnięciu.
Zależności: tylko `utils` (undo, navigation). Testy: `cars/tests.py`.
