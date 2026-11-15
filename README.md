# Blender Assembler

Assemble player truck configurations automatically from game definition files.

The add-on provides a user interface to easily select a cabin, chassis and accessories - using ConverterPIX and SCS Blender Tools, each model is converted and imported, then positioned relative to its accessory model locator. Accessories can be selected manually or a dealer configuration file can be loaded from the game's data, which automatically populates the cabin, chassis and accessories. This is significantly more efficient compared with a traditional workflow, which would involve locating the path of and positioning each accessory.

## Prerequisites

To ensure the add-on functions properly, you must have:
- [Blender](https://www.blender.org/)
- [ConverterPIX](https://github.com/mwl4/ConverterPIX)
- [Euro Truck Simulator 2](https://eurotrucksimulator2.com/)
- [SCS Blender Tools](https://github.com/SCSSoftware/BlenderTools)

## Installation

To install the add-on:
1. Download the [latest release](https://github.com/tomempson/Blender-Assembler/releases/latest) archive - `blender_assembler.zip`,
2. Download the prerequisites outlined above - if you have [ConverterPIXWrapper](https://github.com/simon50keda/ConverterPIXWrapper) installed, the add-on may automatically detect the `converter_pix.exe` executable,
3. Open Blender and navigate to *Edit* > *Preferences* > *Add-ons* > *Install from disk...* and install `blender-assembler.zip`,
4. Set your game path - this can be found by opening Steam, finding Euro Truck Simulator 2 in your library and navigating to *Properties...* > *Installed Files* >  *Browse*,
5. Set your ConverterPIX path, which should point to the `converter_pix.exe` executable,
6. Set your cache path - while this is generated automatically, you can adjust the path if you wish,
7. Set your language, which is used to display cabin, chassis and accessory names - while this is set to English (United Kingdom) by default, you can adjust it to any language found in the `locale.scs` archive,
8. Set the number of parallel conversions that can occur (number of models that can be converted simultaneously).

Blender Assembler should now be installed. The configurations above can also be found in the *Tool* sidebar, under the *Blender Assembler* dropdown.

## Usage

### Core Features

To use the add-on:
- Open Blender and navigate to *View* > *Sidebar* and, if not already enabled, enable it,
- Within the sidebar, click *Blender Assembler*,
- In the *Setup* dropdown, you can;
  - Select a truck, which determines the choice of cabins, chassis and accessories,
  - Choose between an EU or UK configuration, which determines the positioning of `f_mirror` and `s_mirror` accessories - beware, if you opt to use a dealer configuration file, once loaded, it will reset this option to an EU configuration,
  - Enable the enforcing of compatibility, which filters incompatible choices,
  - Enable the enforcing of defaults, which applies the `defaults[]` value assigned in game data,
  - Select a dealer configuration file for the defined truck, which will populate the cabin, chassis and accessories once loaded,
  - Clear all configurations using the *Reset* button.
 
  <img width="368" height="478" alt="Blender Assembler UI - Setup." src="https://github.com/user-attachments/assets/9e5c2a96-f501-4c43-8374-21a2eaa09619" />

- In the *Cabin* dropdown, you can:
  - Select a cabin, based on the defined truck - if you are unable to detect any cabins, try disabling *Enforce Compatibility*.
 
  <img width="368" height="269" alt="Blender Assembler UI - Cabin." src="https://github.com/user-attachments/assets/2a003653-edc7-447e-a0b1-7a40c4b1b31e" />

- In the *Chassis* dropdown, you can:
  - Select a chassis, based on the defined truck - if you are unable to detect any chassis, try disabling *Enforce Compatibility*.
 
  <img width="368" height="269" alt="Blender Assembler UI - Chassis." src="https://github.com/user-attachments/assets/f53b9183-1304-43cb-9e44-8358d7efb38d" />

- In the *Accessories* dropdown, you can:
  - Select accessories, based on the defined truck - if you are unable to detect any accessories, try disabling *Enforce Compatibility*,
  - Search for a specific accessory.
 
  <img width="368" height="500" alt="Blender Assembler UI - Accessories." src="https://github.com/user-attachments/assets/ac5cdfde-dcf6-4df5-ba24-e8ee8f435414" />

- In the *Assemble* dropdown, you can:
  - Convert and import each cabin, chassis and accessory model,
  - Position each cabin, chassis and accessory model relative to its accessory model locator.
 
  <img width="368" height="298" alt="Blender Assembler UI - Assemble." src="https://github.com/user-attachments/assets/96afd81e-977c-4660-ba70-fc05d121eb4a" />

### Custom Top-Level Paths

If you choose to mount your own archive (see below) for use with the add-on, you may find a different top-level path is used. For instance, the folder structure of `def.scs` looks like this:

```
/def/vehicle/truck_dealer/...
```

However, you may wish to use an archive with a folder structure that looks something like this:

```
/base/def/vehicle/truck_dealer/...
```

In this case, the add-on would be unable to access the archive properly - setting a custom top-level path informs the add-on to look inside of said folder, enabling it to work properly. You can set as many paths as you wish.

<img width="657" height="422" alt="Blender Assembler UI - Custom Top-Level Paths." src="https://github.com/user-attachments/assets/6822630b-7cd2-4be6-9ce6-7f113366525f" />

### Archives

When you initially set your game path, the add-on mounts every archive from the game - you can adjust the priority of archives, with the highest priority at the top of the list. If you wish to mount your own archive, simply use the *Mount Archive* button.

<img width="655" height="557" alt="Blender Assembler UI - Archives." src="https://github.com/user-attachments/assets/5f555adf-2dda-423e-8666-83fba23d6847" />

## Bugs

Since this add-on has only recently been released, you may experience issues. To report a bug, please open an issue.

## Limitations

At present, the add-on relies on the dealer configuration file of any one truck - currently, there is no such fallback. Additionally, the add-on can only convert and import exterior models. In the future, there is hope to bring functionality for desktop configuration files, as well as the ability to convert and import interior models. Optimistically, it would be ideal to allow the add-on to work with a wider range of game data, such as trailers and the newly released coaches.

## Disclaimer

Euro Truck Simulator 2 and SCS Software are trademarks of SCS Software. This project
is not affiliated with or endorsed by SCS Software. No game files are included; you
need your own copy of the game in order to use this add-on.