from django.shortcuts import render, redirect, get_object_or_404
from django.views import View
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Sum, Avg
from .models import Cars
from .forms import CarForm, FuelForm, ServiceForm, TyreForm

# 1. GARAŻ (Lista aut)
class GarageView(LoginRequiredMixin, View):
    def get(self, request):
        cars = Cars.objects.filter(user=request.user)
        return render(request, 'cars/garage.html', {'cars': cars})

# 2. DODAWANIE AUTA
class AddCarView(LoginRequiredMixin, View):
    def get(self, request):
        form = CarForm()
        return render(request, 'cars/form_generic.html', {'form': form, 'title': 'Dodaj Samochód'})

    def post(self, request):
        form = CarForm(request.POST)
        if form.is_valid():
            car = form.save(commit=False)
            car.user = request.user
            car.save()
            return redirect('cars:garage')
        return render(request, 'cars/form_generic.html', {'form': form, 'title': 'Dodaj Samochód'})

# 3. KOKPIT (Główny widok ze szczegółami)
class CarDashboardView(LoginRequiredMixin, View):
    def get(self, request, car_id):
        car = get_object_or_404(Cars, id=car_id, user=request.user)
        
        # Pobieranie danych
        fuel_logs = car.fuel_consumptions.all().order_by('-date')
        services = car.services.all().order_by('-date')
        tyres = car.tyres.all().order_by('-purchase_date')

        # Statystyki
        total_fuel = fuel_logs.aggregate(Sum('price'))['price__sum'] or 0
        total_service = services.aggregate(Sum('cost'))['cost__sum'] or 0
        total_tyres = tyres.aggregate(Sum('price'))['price__sum'] or 0
        total_cost = total_fuel + total_service + total_tyres
        
        avg_consumption = fuel_logs.aggregate(Avg('consumption'))['consumption__avg'] or 0

        context = {
            'car': car,
            'fuel_logs': fuel_logs,
            'services': services,
            'tyres': tyres,
            'total_cost': total_cost,
            'avg_consumption': avg_consumption,
        }
        return render(request, 'cars/dashboard.html', context)

# 4. DODAWANIE WPISÓW (Paliwo, Serwis, Opony)
# Przykład dla Paliwa (resztę robi się analogicznie)
class AddFuelView(LoginRequiredMixin, View):
    def get(self, request, car_id):
        car = get_object_or_404(Cars, id=car_id, user=request.user)
        form = FuelForm()
        return render(request, 'cars/form_generic.html', {'form': form, 'title': f'Tankowanie: {car.brand}'})

    def post(self, request, car_id):
        car = get_object_or_404(Cars, id=car_id, user=request.user)
        form = FuelForm(request.POST)
        if form.is_valid():
            log = form.save(commit=False)
            log.car = car
            # Opcjonalnie: Aktualizuj przebieg auta przy tankowaniu
            if log.odometer > car.odometer:
                car.odometer = log.odometer
                car.save()
            log.save()
            return redirect('cars:dashboard', car_id=car.id)
        return render(request, 'cars/form_generic.html', {'form': form, 'title': 'Dodaj Tankowanie'})