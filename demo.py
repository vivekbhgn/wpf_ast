"""
demo.py
-------
End-to-end demo: builds an in-memory WPF AST graph from sample code
and runs every agent tool against it — no OpenAI key needed.
"""

from __future__ import annotations
import json
from pathlib import Path
import tempfile, os

from parsers.csharp_parser import CSharpParser
from parsers.xaml_parser   import XamlParser
from graph_builder         import WpfAstGraph
import agent_tools         as tools_module
from agent_tools import (
    find_component, get_related_components, get_direct_dependencies,
    get_dependents, get_call_chain, get_xaml_bindings,
    get_inheritance_chain, find_by_attribute, summarize_component,
    search_components, get_impact_analysis, export_subgraph_dot,
    get_graph_stats,
)


# ── Sample WPF source code ────────────────────────────────────────────────────

SAMPLE_CS_FILES = {
    "ICustomerService.cs": """\
namespace MyApp.Services {
    public interface ICustomerService {
        Task<IEnumerable<Customer>> GetAllAsync();
        Task<Customer> GetByIdAsync(int id);
        Task<Customer> SaveAsync(Customer customer);
        Task DeleteAsync(int id);
    }
}
""",
    "CustomerService.cs": """\
using MyApp.Models;
using MyApp.Data;
namespace MyApp.Services {
    public class CustomerService : ICustomerService {
        private readonly AppDbContext _db;
        public CustomerService(AppDbContext db) { _db = db; }
        public async Task<IEnumerable<Customer>> GetAllAsync() {
            return await _db.Customers.ToListAsync();
        }
        public async Task<Customer> GetByIdAsync(int id) {
            return await _db.Customers.FindAsync(id);
        }
        public async Task<Customer> SaveAsync(Customer customer) {
            _db.Customers.Update(customer);
            await _db.SaveChangesAsync();
            return customer;
        }
        public async Task DeleteAsync(int id) {
            var c = await GetByIdAsync(id);
            if (c != null) { _db.Customers.Remove(c); await _db.SaveChangesAsync(); }
        }
    }
}
""",
    "BaseViewModel.cs": """\
using System.ComponentModel;
using System.Runtime.CompilerServices;
namespace MyApp.ViewModels {
    public abstract class BaseViewModel : INotifyPropertyChanged {
        public event PropertyChangedEventHandler PropertyChanged;
        protected void SetProperty<T>(ref T field, T value,
                [CallerMemberName] string name = null) {
            field = value;
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
        }
    }
}
""",
    "CustomerViewModel.cs": """\
using System.Collections.ObjectModel;
using System.Windows.Input;
using MyApp.Services;
using MyApp.Models;
namespace MyApp.ViewModels {
    public class CustomerViewModel : BaseViewModel {
        private readonly ICustomerService _customerService;
        private readonly IOrderService _orderService;
        private string _fullName;
        public string FullName {
            get => _fullName;
            set => SetProperty(ref _fullName, value);
        }
        private string _email;
        public string Email {
            get => _email;
            set => SetProperty(ref _email, value);
        }
        private bool _isActive = true;
        public bool IsActive {
            get => _isActive;
            set => SetProperty(ref _isActive, value);
        }
        private Customer _selectedCustomer;
        public Customer SelectedCustomer {
            get => _selectedCustomer;
            set => SetProperty(ref _selectedCustomer, value);
        }
        public ObservableCollection<Customer> Customers { get; } = new();
        public ObservableCollection<Order> Orders { get; } = new();
        public ICommand SaveCommand { get; }
        public ICommand DeleteCommand { get; }
        public ICommand LoadCustomersCommand { get; }
        public ICommand AddOrderCommand { get; }
        public CustomerViewModel(ICustomerService customerService,
                                  IOrderService orderService) {
            _customerService = customerService;
            _orderService    = orderService;
            SaveCommand             = new AsyncRelayCommand(SaveAsync);
            DeleteCommand           = new AsyncRelayCommand(DeleteAsync);
            LoadCustomersCommand    = new AsyncRelayCommand(LoadCustomersAsync);
            AddOrderCommand         = new AsyncRelayCommand(AddOrderAsync);
        }
        public async Task SaveAsync() {
            var c = new Customer { FullName = FullName, Email = Email, IsActive = IsActive };
            await _customerService.SaveAsync(c);
            await LoadCustomersAsync();
        }
        public async Task DeleteAsync() {
            if (SelectedCustomer != null)
                await _customerService.DeleteAsync(SelectedCustomer.Id);
        }
        public async Task LoadCustomersAsync() {
            var list = await _customerService.GetAllAsync();
            Customers.Clear();
            foreach (var c in list) Customers.Add(c);
        }
        public async Task AddOrderAsync() {
            var order = new Order { CustomerId = SelectedCustomer?.Id ?? 0 };
            await _orderService.CreateAsync(order);
        }
    }
}
""",
    "OrderViewModel.cs": """\
using MyApp.Services;
using MyApp.Models;
namespace MyApp.ViewModels {
    public class OrderViewModel : BaseViewModel {
        private readonly IOrderService _orderService;
        private Order _selectedOrder;
        public Order SelectedOrder {
            get => _selectedOrder;
            set => SetProperty(ref _selectedOrder, value);
        }
        public System.Collections.ObjectModel.ObservableCollection<Order> Orders { get; } = new();
        public System.Windows.Input.ICommand RefreshCommand { get; }
        public System.Windows.Input.ICommand DeleteOrderCommand { get; }
        public OrderViewModel(IOrderService orderService) {
            _orderService      = orderService;
            RefreshCommand     = new AsyncRelayCommand(LoadOrdersAsync);
            DeleteOrderCommand = new AsyncRelayCommand(DeleteOrderAsync);
        }
        public async Task LoadOrdersAsync() {
            var list = await _orderService.GetAllAsync();
            Orders.Clear();
            foreach (var o in list) Orders.Add(o);
        }
        public async Task DeleteOrderAsync() {
            if (SelectedOrder != null)
                await _orderService.DeleteAsync(SelectedOrder.Id);
        }
    }
}
""",
    "IOrderService.cs": """\
namespace MyApp.Services {
    public interface IOrderService {
        Task<IEnumerable<Order>> GetAllAsync();
        Task<Order> CreateAsync(Order order);
        Task DeleteAsync(int id);
    }
}
""",
}

SAMPLE_XAML_FILES = {
    "CustomerFormView.xaml": """\
<Window x:Class="MyApp.Views.CustomerFormView"
        xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        DataContext="{Binding CustomerViewModel}">
  <Grid Margin="16">
    <StackPanel x:Name="FormPanel">
      <TextBox x:Name="FullNameBox"
               Text="{Binding FullName, Mode=TwoWay, UpdateSourceTrigger=PropertyChanged}"/>
      <TextBox x:Name="EmailBox"
               Text="{Binding Email, Mode=TwoWay}"/>
      <CheckBox x:Name="ActiveCheck"
                IsChecked="{Binding IsActive, Mode=TwoWay}"
                Content="Active"/>
      <DataGrid x:Name="OrdersGrid"
                ItemsSource="{Binding Orders}"
                SelectedItem="{Binding SelectedCustomer, Mode=TwoWay}"/>
      <Button x:Name="SaveBtn"
              Content="Save"
              Command="{Binding SaveCommand}"
              Click="OnSave_Click"/>
      <Button x:Name="DeleteBtn"
              Content="Delete"
              Command="{Binding DeleteCommand}"/>
    </StackPanel>
  </Grid>
</Window>
""",
    "OrderListView.xaml": """\
<UserControl x:Class="MyApp.Views.OrderListView"
             xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
             xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
             DataContext="{Binding OrderViewModel}">
  <Grid>
    <DataGrid x:Name="OrderGrid"
              ItemsSource="{Binding Orders}"
              SelectedItem="{Binding SelectedOrder, Mode=TwoWay}"
              SelectionChanged="OnOrderSelected"/>
    <Button x:Name="RefreshBtn"
            Content="Refresh"
            Command="{Binding RefreshCommand}"/>
    <Button x:Name="DeleteOrderBtn"
            Content="Delete"
            Command="{Binding DeleteOrderCommand}"/>
  </Grid>
</UserControl>
""",
}


# ── Build graph in a temp directory ──────────────────────────────────────────

def build_demo_graph() -> WpfAstGraph:
    with tempfile.TemporaryDirectory() as tmp:
        for name, src in SAMPLE_CS_FILES.items():
            Path(tmp, name).write_text(src, encoding='utf-8')
        for name, src in SAMPLE_XAML_FILES.items():
            Path(tmp, name).write_text(src, encoding='utf-8')
        graph = WpfAstGraph.from_directory(tmp)
    return graph


# ── Demo runner ───────────────────────────────────────────────────────────────

def hr(title: str = "") -> None:
    print(f"\n{'─'*60}")
    if title:
        print(f"  {title}")
    print('─'*60)

def show(raw: str, max_lines: int = 25) -> None:
    lines = raw.splitlines()
    for l in lines[:max_lines]:
        print(l)
    if len(lines) > max_lines:
        print(f"  ... ({len(lines)-max_lines} more lines)")


def run_demo():
    print("╔══════════════════════════════════════════════════════╗")
    print("║    WPF AST Graph — Agent Tool Demo                  ║")
    print("╚══════════════════════════════════════════════════════╝")

    graph = build_demo_graph()
    tools_module.init_tools(graph)

    # ── Tool 1: graph stats
    hr("1. Graph statistics")
    show(get_graph_stats.invoke({}))

    # ── Tool 2: find_component
    hr("2. find_component('CustomerViewModel')")
    show(find_component.invoke({"name": "CustomerViewModel"}))

    # ── Tool 3: get_related_components
    hr("3. get_related_components('CustomerViewModel', depth=2)")
    show(get_related_components.invoke({"name": "CustomerViewModel", "depth": 2}))

    # ── Tool 4: get_direct_dependencies
    hr("4. get_direct_dependencies('CustomerViewModel')")
    show(get_direct_dependencies.invoke({"class_name": "CustomerViewModel"}))

    # ── Tool 5: get_dependents
    hr("5. get_dependents('ICustomerService')")
    show(get_dependents.invoke({"class_name": "ICustomerService"}))

    # ── Tool 6: get_call_chain
    hr("6. get_call_chain('SaveAsync')")
    show(get_call_chain.invoke({"method_name": "SaveAsync"}))

    # ── Tool 7: get_xaml_bindings
    hr("7. get_xaml_bindings('CustomerFormView')")
    show(get_xaml_bindings.invoke({"viewmodel_or_view_name": "CustomerFormView"}))

    # ── Tool 8: get_inheritance_chain
    hr("8. get_inheritance_chain('BaseViewModel')")
    show(get_inheritance_chain.invoke({"class_name": "BaseViewModel"}))

    # ── Tool 9: find_by_attribute
    hr("9. find_by_attribute('RelayCommand')")
    show(find_by_attribute.invoke({"attribute_name": "RelayCommand"}))

    # ── Tool 10: summarize_component
    hr("10. summarize_component('CustomerViewModel')")
    print(summarize_component.invoke({"name": "CustomerViewModel"}))

    # ── Tool 11: search_components
    hr("11. search_components('order')")
    show(search_components.invoke({"query": "order", "kind_filter": ""}))

    # ── Tool 12: get_impact_analysis
    hr("12. get_impact_analysis('ICustomerService', depth=3)")
    show(get_impact_analysis.invoke({"class_name": "ICustomerService", "depth": 3}))

    # ── Tool 13: export_subgraph_dot
    hr("13. export_subgraph_dot('CustomerViewModel', depth=2) — first 20 lines")
    dot = export_subgraph_dot.invoke({"name": "CustomerViewModel", "depth": 2})
    show(dot)

    print("\n\n✅ Demo complete — all 13 tools executed successfully.")
    print("\nTo run against your project:")
    print("  python agent.py --project /path/to/MyWpfApp --question \"What depends on IOrderService?\"")


if __name__ == "__main__":
    run_demo()
