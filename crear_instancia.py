#!/usr/bin/env python3
"""
Reintento automático de creación de instancia gratuita (A1.Flex) en Oracle Cloud.

Versión adaptada para correr como tarea programada de GitHub Actions: en vez
de un bucle infinito con espera interna, hace UN solo intento por cada dominio
de disponibilidad y termina. Es GitHub Actions (con "schedule") quien se
encarga de volver a ejecutarlo cada cierto tiempo.

Todo lo específico de la cuenta (credenciales, OCIDs, correo) viene de
variables de entorno alimentadas por los "Secrets" del repositorio. El
repositorio es público: no escribas aquí ningún identificador de la cuenta y
no imprimas en el log nada que no deba verse públicamente.
"""

import os
import smtplib
import sys
from datetime import datetime
from email.mime.text import MIMEText

import oci
from oci.core import ComputeClient
from oci.core.models import (
    LaunchInstanceDetails,
    LaunchInstanceShapeConfigDetails,
    CreateVnicDetails,
    InstanceSourceViaImageDetails,
)
from oci.exceptions import ServiceError

# ----------------------------------------------------------------------
# CONFIG genérica (no identifica la cuenta). Lo específico va en Secrets.
# ----------------------------------------------------------------------
IMAGEN_SO = "Canonical Ubuntu"
IMAGEN_VERSION = "22.04"

INSTANCE_NAME = "instance-auto"
SHAPE = "VM.Standard.A1.Flex"
OCPUS = 2
MEMORY_GB = 12

# Errores que significan "sigue intentando"; cualquier otro error se detiene.
# Incluye límite de peticiones (429): con el cron cada 15 min ya es poco
# probable, pero si pasa no debe tratarse como error de configuración.
RETRYABLE_MESSAGES = (
    "Out of capacity",
    "Out of host capacity",
    "OutOfCapacity",
    "InternalError",
    "Too many requests",
    "TooManyRequests",
)
# ----------------------------------------------------------------------


def log(msg: str) -> None:
    marca = datetime.now().strftime("%H:%M:%S")
    print(f"[{marca}] {msg}", flush=True)


def variable_requerida(nombre: str) -> str:
    valor = os.environ.get(nombre)
    if not valor:
        raise RuntimeError(f"Falta la variable de entorno {nombre} (revisa los Secrets del repositorio).")
    return valor


def construir_config_oci() -> dict:
    """Arma la configuración de autenticación de OCI a partir de variables de
    entorno, en vez de leer ~/.oci/config y un archivo .pem del disco."""
    return {
        "user": variable_requerida("OCI_USER_OCID"),
        "tenancy": variable_requerida("OCI_TENANCY_OCID"),
        "fingerprint": variable_requerida("OCI_FINGERPRINT"),
        "region": variable_requerida("OCI_REGION"),
        "key_content": variable_requerida("OCI_PRIVATE_KEY"),
    }


def enviar_notificacion(asunto: str, cuerpo: str) -> None:
    """Manda un correo de aviso por Gmail. Si falla, solo lo registra (no detiene el script)."""
    password = os.environ.get("GMAIL_APP_PASSWORD")
    correo = os.environ.get("NOTIFY_EMAIL")
    if not password or not correo:
        log("Aviso: GMAIL_APP_PASSWORD o NOTIFY_EMAIL no configurados, no se envía correo.")
        return
    try:
        mensaje = MIMEText(cuerpo)
        mensaje["Subject"] = asunto
        mensaje["From"] = correo
        mensaje["To"] = correo
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as servidor:
            servidor.login(correo, password)
            servidor.send_message(mensaje)
        log("Correo de notificación enviado.")
    except Exception as error:
        log(f"No se pudo enviar el correo de notificación: {type(error).__name__}")


def cargar_dominios(identity_client, tenancy_id: str) -> list:
    respuesta = identity_client.list_availability_domains(tenancy_id)
    return [ad.name for ad in respuesta.data]


def buscar_imagen_mas_reciente(compute_client, compartment_id: str) -> str:
    respuesta = compute_client.list_images(
        compartment_id=compartment_id,
        operating_system=IMAGEN_SO,
        operating_system_version=IMAGEN_VERSION,
        shape=SHAPE,
        sort_by="TIMECREATED",
        sort_order="DESC",
    )
    if not respuesta.data:
        raise RuntimeError(
            f"No se encontró ninguna imagen de {IMAGEN_SO} {IMAGEN_VERSION} compatible con {SHAPE}."
        )
    imagen = respuesta.data[0]
    log(f"Imagen encontrada: {imagen.display_name}")
    return imagen.id


def intentar_lanzar(
    compute_client, ad_name: str, ssh_key: str, image_id: str, compartment_id: str, subnet_id: str
) -> bool:
    detalles = LaunchInstanceDetails(
        availability_domain=ad_name,
        compartment_id=compartment_id,
        shape=SHAPE,
        display_name=f"{INSTANCE_NAME}-{ad_name[-2:]}",
        shape_config=LaunchInstanceShapeConfigDetails(
            ocpus=OCPUS,
            memory_in_gbs=MEMORY_GB,
        ),
        source_details=InstanceSourceViaImageDetails(image_id=image_id),
        create_vnic_details=CreateVnicDetails(
            subnet_id=subnet_id,
            assign_public_ip=True,
        ),
        metadata={"ssh_authorized_keys": ssh_key},
    )

    try:
        resultado = compute_client.launch_instance(detalles)
        log("¡Instancia creada con éxito! Los detalles se enviaron por correo.")
        enviar_notificacion(
            "Oracle Cloud: instancia creada",
            f"Se creó la instancia en {ad_name}.\nOCID: {resultado.data.id}",
        )
        return True

    except ServiceError as error:
        mensaje = error.message or ""
        if any(clave in mensaje for clave in RETRYABLE_MESSAGES):
            log(f"{mensaje} en {ad_name} — se reintentará en la próxima ejecución programada.")
            return False
        log(f"Error no recuperable (HTTP {error.status}, {error.code}). El detalle se envió por correo.")
        enviar_notificacion(
            "Oracle Cloud: la automatización se detuvo por un error",
            f"Error no recuperable en {ad_name}: {mensaje}",
        )
        raise


def main() -> int:
    config = construir_config_oci()
    compute_client = ComputeClient(config)
    identity_client = oci.identity.IdentityClient(config)

    ssh_key = variable_requerida("SSH_PUBLIC_KEY")
    subnet_id = variable_requerida("OCI_SUBNET_ID")
    compartment_id = config["tenancy"]
    image_id = buscar_imagen_mas_reciente(compute_client, compartment_id)
    dominios = cargar_dominios(identity_client, compartment_id)
    log(f"Dominios de disponibilidad a probar en esta ejecución: {dominios}")

    for ad_name in dominios:
        log(f"Probando {ad_name}...")
        try:
            if intentar_lanzar(compute_client, ad_name, ssh_key, image_id, compartment_id, subnet_id):
                log("Listo. Instancia creada, ya no hace falta seguir programando esta tarea.")
                return 0
        except ServiceError:
            log("Error de configuración no recuperable. Revisa los Secrets del repositorio.")
            return 1

    log("Sin cupo en ningún dominio por ahora. Se vuelve a intentar en la próxima ejecución programada.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
