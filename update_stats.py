import requests
from datetime import datetime
from pathlib import Path

FILE_PATH = Path("usage_data.txt")


def add_to_file(
    number_of_files: int,
) -> None:
    """
    Add content to the file

    :param number_of_files: number of files
    :return: None
    """

    data = f"{datetime.now().strftime('%Y-%m-%d')},{number_of_files}\n"

    if FILE_PATH.exists():

        with open(FILE_PATH, "a") as f:
            f.write(data)
        print(f"Added {number_of_files} to {FILE_PATH}")

    else:
        print(f"File {FILE_PATH} does not exist. Creating a new file.")
        with open(FILE_PATH, "w") as f:
            f.write("date,number_of_files\n")
            f.write(data)

        print(f"Created {FILE_PATH} and added {number_of_files}")


def save_number_of_files():
    """
    Save the number in bedbase to path of files to the file
    :return: None
    """

    # Get the number of files from the server
    response = requests.get("https://api.bedbase.org/v1/stats")
    if response.status_code == 200:
        number_of_files = response.json()["bedfiles_number"]
        add_to_file(number_of_files)
    else:
        print(f"Error: {response.status_code}")


if __name__ == "__main__":

    save_number_of_files()
