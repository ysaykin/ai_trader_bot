import reflex as rx

config = rx.Config(
    app_name="reflex_dashboard",
    api_url="http://localhost:8005",
    plugins=[
        rx.plugins.SitemapPlugin(),
        rx.plugins.TailwindV4Plugin(),
        rx.plugins.RadixThemesPlugin(
            theme=rx.theme(
                appearance="dark",
                accent_color="cyan",
                gray_color="slate",
                radius="small",
                scaling="95%",
            )
        ),
    ],
)
