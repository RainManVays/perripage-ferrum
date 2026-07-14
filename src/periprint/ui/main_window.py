import customtkinter as ctk


class MainWindow(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title("PeriPrint")
        self.geometry("900x600")
